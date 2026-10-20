"""v5.27 A1: shared contracts only; no call engine or result persistence."""

import json
from datetime import datetime, timezone
from typing import get_args

import pytest
import yaml
from pydantic import ValidationError

from waitless import models as m
from waitless.config import ConfigError, load_engine_config, load_settings
from waitless.interfaces import ToolFailure
from waitless.mcp_server import TOOL_DESCRIPTIONS, ToolHandlers, build_mcp

from a_support import ENGINE_YAML, make_settings, mcp_app_for, mcp_session, stub_services


TOOLS = (
    "find_official_number", "prepare_task", "update_task", "start_assisted_call",
    "get_call_status", "answer_pending_choice", "join_call", "return_to_agent",
    "get_case_result", "hangup_call", "report_call_result",
)
NEW_TOOLS = ("hangup_call", "report_call_result")
CONFIG_FIELDS = (
    ("handoff", "join_timeout_s"), ("handoff", "rejoin_grace_s"),
    ("speaker", "verdict_timeout_ms"), ("speaker", "output_buffer_ms"),
    ("speaker", "handoff_latency_max_ms"),
)
MISSING = object()
INVALID_INTS = (MISSING, None, True, False, "90", 0, -1, 1.5, 90.0)


def config_payload():
    data = yaml.safe_load(ENGINE_YAML.read_text())
    data["handoff"].update(join_timeout_s=90, rejoin_grace_s=60,
                           notification_text="有人接起了，请马上加入")
    data["speaker"] = dict(verdict_timeout_ms=1000, output_buffer_ms=1200,
                           handoff_latency_max_ms=1500)
    return data


def result_payload(model, **over):
    data = dict(outcome="confirmed", confirmation_number=None, verified=[],
                follow_ups=[], summary="用户填写", result_source="user",
                confirmation_source=None, awaiting_user_result=False)
    if model is m.CaseResultOut:
        data.update(task_id="task-contract", company_name="Company", headline="Done",
                    calls=[], history=[])
    else:
        data["call_id"] = "call-contract"
    return {**data, **over}


def handoff_payload(**over):
    return dict(state="ready", company_name="Company",
                media=dict(ai_media="disconnected", recording=False, transcription=False),
                reason="有人接起了，请马上加入", reason_kind="live_human",
                expires_at=datetime(2026, 10, 20, tzinfo=timezone.utc),
                remaining_s=90, generation=1, **over)


def test_complete_config_and_shipped_values():
    cfg = m.EngineConfig.model_validate(config_payload())
    assert cfg.handoff.join_timeout_s == 90
    assert cfg.handoff.rejoin_grace_s == 60
    assert cfg.speaker.handoff_latency_max_ms == 1500
    shipped = load_engine_config(ENGINE_YAML)
    assert shipped.handoff.join_timeout_s == 90
    assert shipped.handoff.rejoin_grace_s == 60
    assert shipped.speaker.handoff_latency_max_ms == 1500
    assert shipped.handoff.notification_text.strip()
    assert "max_unknown" not in type(shipped.speaker).model_fields


@pytest.mark.parametrize("section,field", CONFIG_FIELDS)
@pytest.mark.parametrize("bad", INVALID_INTS, ids=("missing", "null", "true", "false", "string", "zero", "negative", "float", "integral-float"))
def test_config_strict_required_positive_integers(section, field, bad, tmp_path, mode_env):
    cls = getattr(m, "EngineHandoff" if section == "handoff" else "EngineSpeaker")
    assert field in cls.model_fields
    assert cls.model_fields[field].is_required()
    data = config_payload()
    if bad is MISSING:
        del data[section][field]
    else:
        data[section][field] = bad
    with pytest.raises(ValidationError) as err:
        m.EngineConfig.model_validate(data)
    assert any(e["loc"] == (section, field) for e in err.value.errors())
    path = tmp_path / "engine.yaml"
    path.write_text(yaml.safe_dump(data))
    with pytest.raises(ConfigError) as err:
        load_engine_config(path)
    assert err.value.missing == ("WAITLESS_ENGINE_CONFIG",)
    with pytest.raises(ConfigError):
        load_settings(mode_env("replay", WAITLESS_ENGINE_CONFIG=str(path)))


@pytest.mark.parametrize("bad", (MISSING, None, {}))
def test_speaker_section_required(bad, tmp_path):
    assert "speaker" in m.EngineConfig.model_fields
    assert m.EngineConfig.model_fields["speaker"].is_required()
    data = config_payload()
    if bad is MISSING:
        del data["speaker"]
    else:
        data["speaker"] = bad
    path = tmp_path / "engine.yaml"
    path.write_text(yaml.safe_dump(data))
    with pytest.raises(ConfigError):
        load_engine_config(path)


@pytest.mark.parametrize("bad", (MISSING, None, "", "   ", True, 90))
def test_notification_text_is_required_nonblank_string(bad):
    assert "notification_text" in m.EngineHandoff.model_fields
    assert m.EngineHandoff.model_fields["notification_text"].is_required()
    data = config_payload()
    if bad is MISSING:
        del data["handoff"]["notification_text"]
    else:
        data["handoff"]["notification_text"] = bad
    with pytest.raises(ValidationError):
        m.EngineConfig.model_validate(data)


def test_removed_unknown_counter_is_rejected():
    data = config_payload()
    m.EngineConfig.model_validate(data)
    data["speaker"]["max_unknown"] = 1
    with pytest.raises(ValidationError):
        m.EngineConfig.model_validate(data)


def test_handoff_authoritative_fields_roundtrip_and_text_defaults():
    obj = m.HandoffStatus.model_validate(handoff_payload())
    assert obj.reason_kind == "live_human" and obj.generation == 1
    assert obj.remaining_s == 90 and obj.expires_at.tzinfo is not None
    assert m.HandoffStatus.model_validate_json(obj.model_dump_json()) == obj
    legacy = m.HandoffStatus(state="ready", company_name="Company",
                             media=m.Media(ai_media="connected", recording=False, transcription=True))
    assert legacy.expires_at is None and legacy.remaining_s is None
    assert legacy.reason_kind is None and legacy.generation == 0


@pytest.mark.parametrize("field,bad", (
    ("generation", -1), ("generation", True), ("generation", "1"),
    ("remaining_s", -1), ("remaining_s", True), ("remaining_s", "1"),
    ("reason_kind", "unknown"), ("expires_at", "2026-10-20T00:00:00"),
))
def test_handoff_authoritative_fields_reject_invalid_values(field, bad):
    data = handoff_payload()
    m.HandoffStatus.model_validate(data)
    data[field] = bad
    with pytest.raises(ValidationError):
        m.HandoffStatus.model_validate(data)


@pytest.mark.parametrize("reason", ("returned", "company_hangup", "user_hangup", "join_timeout", "rejoin_timeout"))
def test_handoff_ended_reasons(reason):
    assert m.HandoffEnded(type="ended", reason=reason).reason == reason


@pytest.mark.parametrize("model", (m.CallResult, m.CaseResultOut))
def test_user_confirmed_without_number_and_legacy_transcript_default(model):
    obj = model.model_validate(result_payload(model))
    assert obj.outcome == "confirmed" and obj.result_source == "user"
    assert obj.confirmation_source is None
    assert model.model_validate_json(obj.model_dump_json()) == obj
    data = result_payload(model, confirmation_number="reference")
    for key in ("result_source", "confirmation_source", "awaiting_user_result"):
        del data[key]
    old = model.model_validate(data)
    assert old.result_source == "transcript" and old.confirmation_source == "transcript"
    assert old.awaiting_user_result is False


@pytest.mark.parametrize("model", (m.CallResult, m.CaseResultOut))
@pytest.mark.parametrize("source", ("user", "transcript"))
def test_confirmation_source_with_number(model, source):
    obj = model.model_validate(result_payload(model, result_source=source,
                              confirmation_number="reference", confirmation_source=source))
    assert obj.confirmation_source == source


@pytest.mark.parametrize("model", (m.CallResult, m.CaseResultOut))
@pytest.mark.parametrize("over", (
    dict(result_source="transcript"), dict(result_source="agent"),
    dict(confirmation_source="agent"),
    dict(awaiting_user_result="true"), dict(awaiting_user_result=1),
))
def test_result_sources_and_transcript_confirmation_validation(model, over):
    model.model_validate(result_payload(model))
    with pytest.raises(ValidationError):
        model.model_validate(result_payload(model, **over))


@pytest.mark.parametrize("model", (m.CallResult, m.CaseResultOut))
def test_awaiting_result_and_unconfirmed_transcript_can_have_no_number(model):
    obj = model.model_validate(result_payload(model, outcome="needs_follow_up",
                              result_source="transcript", awaiting_user_result=True))
    assert obj.awaiting_user_result is True and obj.confirmation_source is None


@pytest.mark.parametrize("model", (m.CallResult, m.CaseResultOut))
def test_absent_number_clears_source_including_revalidated_model_copy(model):
    obj = model.model_validate(result_payload(model, confirmation_source="user"))
    assert obj.confirmation_source is None
    original = model.model_validate(result_payload(model, result_source="transcript",
                                    confirmation_number="reference"))
    assert original.confirmation_source == "transcript"
    copied = original.model_copy(update={"outcome": "needs_follow_up", "confirmation_number": None})
    assert model.model_validate(copied).confirmation_source is None
    assert model.model_validate(copied.model_dump()).confirmation_source is None


def test_user_source_does_not_relax_foreign_evidence_validation():
    m.CallResult.model_validate(result_payload(m.CallResult))
    with pytest.raises(ValidationError):
        m.CallResult.model_validate(result_payload(m.CallResult, verified=[dict(
            label="Evidence", value="value", evidence=dict(call_id="different-call", turn_seq=1))]))


def test_eleven_tool_names_io_descriptions_errors_and_user_action_guard():
    assert m.TOOL_NAMES == TOOLS
    assert get_args(m.ToolName) == TOOLS
    assert tuple(m.TOOL_IO) == TOOLS
    assert set(TOOL_DESCRIPTIONS) == set(TOOLS) == set(m.TOOL_ERROR_CODES)
    assert m.TOOL_IO["hangup_call"] == (m.HangupCallIn, m.HangupCallOut)
    assert m.TOOL_IO["report_call_result"] == (m.ReportCallResultIn, m.ReportCallResultOut)
    assert issubclass(m.ReportCallResultOut, m.CallResult)
    for tool in NEW_TOOLS:
        assert tool in m.USER_ACTION_TOOLS
        assert "user" in TOOL_DESCRIPTIONS[tool].lower()
        assert "not_implemented" in m.TOOL_ERROR_CODES[tool]
        with pytest.raises(ValidationError):
            m.NextToolCall(kind="tool_call", tool_use_id="use-contract", name=tool,
                           arguments={"task_id": "task-contract"})
    for code in ("result_conflict", "result_not_allowed"):
        assert code in m.TOOL_ERROR_CODES["report_call_result"]
        assert ToolFailure(code, "contract").to_error().code == code


@pytest.mark.parametrize("status", ("hung_up", "already_ended", "no_active_call"))
def test_hangup_output_model(status):
    cls = getattr(m, "HangupCallOut")
    obj = cls(status=status, call_id=None if status == "no_active_call" else "call-contract",
              remote_hangup_pending=False)
    assert set(obj.model_dump()) == {"status", "call_id", "remote_hangup_pending"}
    for field in cls.model_fields:
        assert cls.model_fields[field].is_required()
    with pytest.raises(ValidationError):
        cls(status=status, call_id=None, remote_hangup_pending="false")


@pytest.mark.parametrize("data", (
    dict(status="hung_up", call_id=None, remote_hangup_pending=False),
    dict(status="already_ended", call_id=None, remote_hangup_pending=False),
    dict(status="no_active_call", call_id="call-contract", remote_hangup_pending=False),
    dict(status="no_active_call", call_id=None, remote_hangup_pending=True),
    dict(status="resuming", call_id="call-contract", remote_hangup_pending=False),
))
def test_hangup_output_status_call_id_consistency(data):
    cls = getattr(m, "HangupCallOut")
    with pytest.raises(ValidationError):
        cls.model_validate(data)


@pytest.mark.parametrize("outcome", ("confirmed", "failed", "needs_follow_up"))
def test_report_result_input_and_output(outcome):
    cls = getattr(m, "ReportCallResultIn")
    obj = cls(task_id="task-contract", call_id="call-contract", outcome=outcome)
    assert obj.confirmation_number is None
    saved = m.ReportCallResultOut.model_validate(result_payload(m.CallResult, outcome=outcome))
    assert saved.outcome == outcome
    for bad in ("in_progress", "success", None):
        with pytest.raises(ValidationError):
            cls(task_id="task-contract", call_id="call-contract", outcome=bad)
    for field in ("task_id", "call_id"):
        data = obj.model_dump()
        data[field] = ""
        with pytest.raises(ValidationError):
            cls.model_validate(data)


@pytest.mark.parametrize("tool,args", (
    ("hangup_call", dict(task_id="task-contract")),
    ("report_call_result", dict(task_id="task-contract", call_id="call-contract", outcome="confirmed")),
))
async def test_new_handlers_and_mcp_return_business_error(tool, args, tmp_path):
    services = stub_services(make_settings(tmp_path))
    from types import SimpleNamespace
    from unittest.mock import AsyncMock
    from waitless.interfaces import ToolFailure
    calls = SimpleNamespace(hangup=AsyncMock(side_effect=ToolFailure("unknown_task")),
                            report_result=AsyncMock(side_effect=ToolFailure("unknown_task")))
    handlers = ToolHandlers(services, parts=SimpleNamespace(calls=calls))
    with pytest.raises(ToolFailure, match="unknown_task"):
        await getattr(handlers, tool)(**args)
    server = build_mcp(services, handlers=handlers)
    async with mcp_session(mcp_app_for(server)) as client:
        tools = {t.name: t for t in (await client.list_tools()).tools}
        assert set(tools) == set(TOOLS)
        for name in NEW_TOOLS:
            inp, out = m.TOOL_IO[name]
            schema = tools[name].input_schema
            assert set(schema["properties"]) == set(inp.model_fields)
            assert set(schema.get("required", [])) == set(inp.model_json_schema().get("required", []))
            assert schema["properties"] == inp.model_json_schema()["properties"]
            assert set(tools[name].output_schema["properties"]) == set(out.model_fields)
        result = await client.call_tool(tool, args)
    assert result.is_error is True
    assert json.loads(result.content[0].text)["code"] == "unknown_task"


def test_speaker_policy_rule_trace_and_unchanged_text_return():
    event = m.TraceEvent(task_id="task-contract", call_id="call-contract", event_type="decision",
                        decision_id="decision-contract", decision_point="agent_tool",
                        policy_decision={"policy_rule": "phone-agent/speaker", "action": "handoff"})
    assert event.policy_decision["policy_rule"] == "phone-agent/speaker"
    obj = m.ReturnOut(task_id="task-contract", call_id="call-contract", status="resuming",
                      media=dict(ai_media="connected", recording=False, transcription=True))
    assert obj.status == "resuming"
