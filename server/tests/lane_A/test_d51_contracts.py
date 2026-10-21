"""D51 E133 contracts and real service/trace integration."""

from dataclasses import fields
from typing import get_args

from a_support import AEnv, fake_compiled, make_call_deps, sample_card
from fakes import FakeConverse, FakeJev
from waitless import models as m
from waitless.app import build_services
from waitless.config import load_settings
from waitless.interfaces import CallDeps


def test_d51_trace_event_types():
    assert {"agent_tool_call", "agent_tool_result", "status_updated"} <= set(get_args(m.TraceEventType))


def test_d51_trace_action_keys():
    assert m.TRACE_ACTION_KEYS["agent_tool_call"] == ("tool", "tool_call_id", "args")
    assert m.TRACE_ACTION_KEYS["agent_tool_result"] == ("tool", "tool_call_id", "ok", "result", "latency_ms")
    assert m.TRACE_ACTION_KEYS["status_updated"] == ("phase", "status_line")


def test_d51_agent_tool_decision_point():
    assert "agent_tool" in get_args(m.DecisionPoint)
    assert "agent_tool" not in m.DECISION_POINT_CODES
    assert "agent_tool" not in m.WAIT_ONLY_POINTS


def test_d51_compiled_call_tool_descriptions_default():
    compiled = fake_compiled(sample_card())
    assert compiled.agent_tool_descriptions == {}
    configured = m.CompiledCall.model_validate({**compiled.model_dump(), "agent_tool_descriptions": {"press_keys": "x"}})
    assert configured.agent_tool_descriptions == {"press_keys": "x"}
    configured.agent_tool_descriptions["ask_jev"] = "question"
    assert compiled.agent_tool_descriptions == {}


def test_d51_calldeps_has_optional_jev(tmp_path):
    by_name = {field.name: field for field in fields(CallDeps)}
    assert "jev" in by_name
    assert by_name["jev"].default is None
    env = AEnv(tmp_path)
    try:
        deps = make_call_deps(env.settings, env.store, env.trace, env.clock)
        assert deps.jev is None
    finally:
        env.close()


def test_d51_build_services_puts_gate_jev_into_calldeps(mode_env):
    jev = FakeJev()
    settings = load_settings(mode_env("live-sim"))
    services, parts = build_services(settings, jev=jev, converse=FakeConverse())
    try:
        assert parts.calls.deps.jev is jev
        assert parts.calls.deps.gate._jev is jev
    finally:
        services.store.close()


def test_d51_trace_accepts_agent_tool_decision(tmp_path):
    env = AEnv(tmp_path)
    try:
        env.trace.event(m.TraceEvent(
            task_id="task", call_id="call", event_type="decision", decision_id="d51",
            decision_point="agent_tool", jev_request={"tool": "press_keys", "arguments": {"digits": "1"}},
            jev_response={"answers": {}},
            policy_decision={"intent": "press_keys", "action": "press", "reason_code": "agent_tool", "policy_rule": "phone-agent"},
            action={"turn_seq": 0},
        ))
        env.trace.event(m.TraceEvent(
            task_id="task", call_id="call", event_type="dtmf_sent", decision_id="d51", action={"digits": "1"},
        ))
        for event_type, action in [
            ("agent_tool_call", {"tool": "press_keys", "tool_call_id": "tool-1", "args": {"digits": "1"}}),
            ("agent_tool_result", {"tool": "press_keys", "tool_call_id": "tool-1", "ok": True, "result": {"sent": "1"}, "latency_ms": 1}),
            ("status_updated", {"phase": "ivr", "status_line": "Selecting service"}),
        ]:
            env.trace.event(m.TraceEvent(task_id="task", call_id="call", event_type=event_type, action=action))
        events = env.trace.events(call_id="call")
        assert [event.event_type for event in events] == [
            "decision", "dtmf_sent", "agent_tool_call", "agent_tool_result", "status_updated",
        ]
        assert events[0].decision_point == "agent_tool"
        assert events[1].decision_id == events[0].decision_id == "d51"
        assert events[2].action == {"tool": "press_keys", "tool_call_id": "tool-1", "args": {"digits": "1"}}
        assert events[3].action == {"tool": "press_keys", "tool_call_id": "tool-1", "ok": True, "result": {"sent": "1"}, "latency_ms": 1}
        assert events[4].action == {"phase": "ivr", "status_line": "Selecting service"}
        assert env.trace.check_call("call") == []
    finally:
        env.close()
