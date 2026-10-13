"""AC-70：动作执行器每次都要带一个已记录的 decision_id；decision_id 不存在时拒绝执行。

机制（tech §7.1 AC-70）：LiveActionBackend.execute 先 deps.trace.has_decision(result.decision_id)，否则抛
UnknownDecision（interfaces 定义，call/actions.py 再导出），且不做任何动作。
另测动作到协作者的接线（§6.5 动作表、§7.1 AC-69）：say 注入并把 OutputGuard 切出 conversation；release 调
OutputGuard.release(result.turn_seq)（缺口 D-G-2）；wait 不动作。协作者是只记录调用的替身（方法名取自骨架）；
LiveActionBackend 的 call_sid 是必填关键字参数（D-G-1）。
"""

from types import SimpleNamespace
from typing import Any

import pytest

from call_engine._support import (
    FakeCarrierApi,
    FakeGuard,
    FakeInjector,
    FakeMediaGate,
    FakePipelineTask,
    TEST_CALL_SID,
    drive,
    finish_or_cancel,
    make_result,
    record_decision,
    run_for,
)
from waitless import interfaces
from waitless import models as m

CONSEQUENTIAL = tuple(sorted(m.CONSEQUENTIAL_EVENTS))


def backend(spec: Any, deps: Any) -> SimpleNamespace:
    from waitless.call.actions import LiveActionBackend

    env = SimpleNamespace(
        injector=FakeInjector(), guard=FakeGuard(), media_gate=FakeMediaGate(), task=FakePipelineTask(), carrier=FakeCarrierApi()
    )
    env.actions = LiveActionBackend(
        spec=spec, deps=deps, injector=env.injector, output_guard=env.guard, media_gate=env.media_gate,
        pipeline_task=env.task, carrier=env.carrier, call_sid=TEST_CALL_SID,
    )
    env.snapshot = {name: len(getattr(env, name).calls) for name in ("injector", "guard", "media_gate", "carrier")}
    return env


def new_calls(env: SimpleNamespace) -> dict[str, list[str]]:
    return {name: getattr(env, name).names()[n:] for name, n in env.snapshot.items()}


UNRECORDED = [
    ("press", dict(digits="1", intent="1", call_phase="ivr_menu")),
    ("say", dict(say_text="We'll keep the deposit option.", intent="answer_precall:deposit_or_autopay")),
    ("release", dict(intent="open_answer")),
    ("ask_user", dict(intent="ask_user")),
    ("handoff", dict(intent="request_handoff", call_phase="live_rep")),
    ("end", dict(intent="confirm_and_end")),
]


@pytest.mark.parametrize("action, fields", UNRECORDED, ids=[a for a, _ in UNRECORDED])
async def test_ac70_unrecorded_decision_is_refused_without_side_effects(
    action: str, fields: dict[str, Any], spec: Any, deps: Any, trace: Any, fake_clock: Any
) -> None:
    from waitless.call.actions import UnknownDecision

    assert UnknownDecision is interfaces.UnknownDecision
    env = backend(spec, deps)
    result = make_result(action, **fields)  # 没有写 decision 事件
    with pytest.raises(UnknownDecision) as info:
        await drive(env.actions.execute(result), fake_clock)
    assert info.value.decision_id == result.decision_id
    assert new_calls(env) == {"injector": [], "guard": [], "media_gate": [], "carrier": []}
    assert env.task.frames == []
    assert trace.of_type(*CONSEQUENTIAL) == []
    assert deps.pending.created == []
    assert deps.on_ended.calls == []


async def test_ac70_recorded_decision_is_executed_with_its_id(spec: Any, deps: Any, trace: Any, fake_clock: Any) -> None:
    env = backend(spec, deps)
    result = record_decision(
        trace, make_result("say", say_text="We'll keep the deposit option.", intent="answer_precall:deposit_or_autopay"),
        task_id=spec.task_id, call_id=spec.call_id, turn_seq=4,
    )
    task = await run_for(env.actions.execute(result), fake_clock)  # say 之后可以等 ASSISTANT FINAL，也可以立即返回
    record = await finish_or_cancel(task)
    if record is not None:
        assert isinstance(record, m.ActionRecord)
        assert (record.decision_id, record.action) == (result.decision_id, "say")
    injects = [c for c in env.injector.calls if c[0] == "inject"]
    assert len(injects) == 1
    _, args, _ = injects[0]
    assert args[0] == result.say_text and args[1] == result.decision_id
    assert trace.violations == []


async def test_ac69_say_moves_the_guard_out_of_conversation(spec: Any, deps: Any, trace: Any, fake_clock: Any) -> None:
    """§7.1 AC-69：决定不是 release 时调 OutputGuard.set_state()，切到 injected（扣住的回应随之丢弃）。"""
    env = backend(spec, deps)
    env.guard.set_state("conversation")
    result = record_decision(trace, make_result("say", say_text="No thank you.", intent="decline_offer"),
                             task_id=spec.task_id, call_id=spec.call_id, turn_seq=2)
    await finish_or_cancel(await run_for(env.actions.execute(result), fake_clock))
    states = [c[1][0] for c in env.guard.calls if c[0] == "set_state"]
    assert "injected" in states
    assert not [c for c in env.guard.calls if c[0] == "release"]


async def test_ac69_release_decision_releases_the_held_response(spec: Any, deps: Any, trace: Any, fake_clock: Any) -> None:
    env = backend(spec, deps)
    env.guard.set_state("conversation")
    result = record_decision(trace, make_result("release", intent="open_answer"), task_id=spec.task_id, call_id=spec.call_id, turn_seq=6)
    assert result.turn_seq == 6
    record = await drive(env.actions.execute(result), fake_clock)
    assert record.action == "release" and record.decision_id == result.decision_id
    assert [c[1] for c in env.guard.calls if c[0] == "release"] == [(6,)]  # D-G-2：放出的是 result.turn_seq 这一轮
    assert [c for c in env.injector.calls if c[0] == "inject"] == []
    assert env.task.dtmf() == []


async def test_ac70_wait_decision_does_nothing(spec: Any, deps: Any, trace: Any, fake_clock: Any) -> None:
    env = backend(spec, deps)
    result = record_decision(trace, make_result("wait", intent="wait"), task_id=spec.task_id, call_id=spec.call_id, turn_seq=1)
    record = await drive(env.actions.execute(result), fake_clock)
    assert (record.decision_id, record.action, record.ok) == (result.decision_id, "wait", True)
    assert [n for n in new_calls(env)["injector"] if n == "inject"] == []
    assert new_calls(env)["media_gate"] == [] and new_calls(env)["carrier"] == []
    assert env.task.frames == []
    assert trace.of_type(*CONSEQUENTIAL) == []
