"""AC-72（D 侧；G 侧的策略在 G 线测）：注入后，Jev said_as_decided 低于 0.7 时重新注入一次；仍低于 0.7 时
走安全默认，并记 say_mismatch。

A-02 定稿的接口：注入后的下一段 ASSISTANT FINAL（SonicTap 事件，经 TranscriptTap 交给
LiveActionBackend.on_assistant_final）→ deps.gate.decide("said_as_decided", DecisionRequest(..., attempt=核对次数))。
G 返回：通过 → action=wait、intent=said_ok；第一次未过 → action=say、say_text=原注入文字、intent=reinject；
第二次仍未过 → policy 的 said_as_decided.safe_default 动作（建议 end），reason_code=say_mismatch。
D 照返回的动作执行（decision_id 是 DP-C2 那条决策），safe_default 时写 say_mismatch 事件；
template_vars[models.SAID_SUMMARY_VAR] 是本次注入的文字（不含前缀）。D 不自己比阈值（D10、D11）。

这里的 G 由 _support.FakeGate 扮演：按 request.attempt 与 A-02 编码返回，与 G 的策略同口径。
"""

import asyncio
from typing import Any

from call_engine._support import (
    SAY_TEMPLATES,
    TEST_CALL_SID,
    FakeGate,
    MiniCall,
    make_deps,
    make_result,
    record_decision,
    run_for,
    settle,
)
from fakes import SonicResponse
from waitless import models as m

INSTRUCTION = "Please keep the deposit option on the account."


def say_decision(trace: Any, spec: Any) -> Any:
    return record_decision(
        trace, make_result("say", say_text=INSTRUCTION, intent="answer_precall:deposit_or_autopay"),
        task_id=spec.task_id, call_id=spec.call_id, turn_seq=5,
    )


def spoken(text: str) -> SonicResponse:
    return SonicResponse(text=text, audio_ms=200, chunk_ms=40)


async def test_ac72_said_as_decided_is_checked_on_the_next_assistant_final(
    spec: Any, deps: Any, engine: Any, make_fake_sonic: Any, trace: Any, gate: Any
) -> None:
    gate.said.extend(["pass"])
    said = "We'd like to keep the deposit option."
    async with MiniCall(spec=spec, deps=deps, engine=engine, make_sonic=make_fake_sonic, responses=[spoken(said)]) as mc:
        await mc.run(say_decision(trace, spec))
    (request,) = gate.said_requests()
    assert request.point == "said_as_decided"
    assert (request.call_id, request.task_id) == (spec.call_id, spec.task_id)
    assert request.transcript and request.transcript[-1].speaker == "waitless"
    assert request.transcript[-1].text == said
    assert request.template_vars[m.SAID_SUMMARY_VAR] == INSTRUCTION
    assert request.attempt == 0
    assert mc.sent_injections() == [INSTRUCTION]
    assert trace.of_type("say_mismatch", call_id=spec.call_id) == []


async def test_ac72_one_low_check_reinjects_exactly_once(
    spec: Any, deps: Any, engine: Any, make_fake_sonic: Any, trace: Any, gate: Any
) -> None:
    gate.said.extend(["fail", "pass"])
    responses = [spoken("We'll switch you to autopay."), spoken("Sorry — please keep the deposit option.")]
    async with MiniCall(spec=spec, deps=deps, engine=engine, make_sonic=make_fake_sonic, responses=responses) as mc:
        await mc.run(say_decision(trace, spec))
    assert mc.sent_injections() == [INSTRUCTION, INSTRUCTION]
    assert len(gate.said_requests()) == 2
    assert trace.of_type("say_mismatch", call_id=spec.call_id) == []
    assert trace.violations == []


async def test_ac72_two_low_checks_take_the_safe_default_and_record_say_mismatch(
    spec: Any, deps: Any, engine: Any, make_fake_sonic: Any, trace: Any, gate: Any
) -> None:
    gate.said.extend(["fail", "fail"])
    gate.safe_action = "end"  # A-02：said_as_decided.safe_default 建议 end
    responses = [spoken("We'll switch you to autopay."), spoken("Autopay it is."), spoken(SAY_TEMPLATES["end.goodbye"])]
    async with MiniCall(spec=spec, deps=deps, engine=engine, make_sonic=make_fake_sonic, responses=responses) as mc:
        await mc.run(say_decision(trace, spec))
    sent = mc.sent_injections()
    assert sent.count(INSTRUCTION) == 2, f"expected the original plus one re-injection, got {sent}"
    mismatches = trace.of_type("say_mismatch", call_id=spec.call_id)
    assert len(mismatches) == 1
    assert SAY_TEMPLATES["end.goodbye"] in sent  # 安全默认（end）的道别话术
    goodbye = [e for e in trace.of_type("injection_sent", call_id=spec.call_id) if (e.action or {}).get("template") == "end.goodbye"]
    assert goodbye and trace.has_decision(goodbye[0].decision_id)
    # D-G-1：end 用本通的 call_sid 挂断，并写带 decision_id 的 hangup
    assert [c[1] for c in mc.carrier.calls if c[0] == "hangup"] == [(TEST_CALL_SID,)]
    hangups = trace.of_type("hangup", call_id=spec.call_id)
    assert len(hangups) == 1 and trace.has_decision(hangups[0].decision_id)
    assert trace.violations == []


async def test_ac72_attempt_counts_the_checks_of_one_injection(
    spec: Any, deps: Any, engine: Any, make_fake_sonic: Any, trace: Any, gate: Any
) -> None:
    """A-02：DecisionRequest.attempt 传核对次数（第一次 0，重新注入后的那次 1）；两次的 decision_summary 都是原注入文字。"""
    gate.said.extend(["fail", "pass"])
    responses = [spoken("We'll switch you to autopay."), spoken("Please keep the deposit option.")]
    async with MiniCall(spec=spec, deps=deps, engine=engine, make_sonic=make_fake_sonic, responses=responses) as mc:
        await mc.run(say_decision(trace, spec))
    assert [r.attempt for r in gate.said_requests()] == [0, 1]
    assert [r.template_vars[m.SAID_SUMMARY_VAR] for r in gate.said_requests()] == [INSTRUCTION, INSTRUCTION]


class SlowGate(FakeGate):
    """DP-C2 的决定迟迟不回（模拟 Jev 网络往返）：decide 等 release 事件。"""

    def __init__(self, trace: Any) -> None:
        super().__init__(trace)
        self.go = asyncio.Event()

    async def decide(self, point: str, request: Any) -> Any:
        if point == "said_as_decided":
            self.point_calls.append((point, request))
            await self.go.wait()
            self.point_calls.pop()
        return await super().decide(point, request)


async def test_ac72_assistant_final_callback_does_not_wait_for_the_check(
    spec: Any, settings: Any, store: Any, engine: Any, make_fake_sonic: Any, trace: Any, fake_clock: Any
) -> None:
    """缺口 D-G-8：SonicTap 回调不得阻塞——DP-C2 决定还没回来时，on_provider_text 已经返回。"""
    gate = SlowGate(trace)
    deps = make_deps(settings=settings, trace=trace, clock=fake_clock, gate=gate, store=store)
    async with MiniCall(spec=spec, deps=deps, engine=engine, make_sonic=make_fake_sonic) as mc:
        task = await run_for(mc.actions.execute(say_decision(trace, spec)), fake_clock, ms=200)
        final = m.ProviderTextEvent(role="ASSISTANT", stage="FINAL", text="We'll keep the deposit option.",
                                    stop_reason="END_TURN", t_ms=fake_clock.monotonic_ms())
        await asyncio.wait_for(mc.tap.on_provider_text(final), 2.0)  # 看门狗（真实时间）：回调必须立即返回
        await settle()
        assert len(gate.point_calls) == 1, "the DP-C2 check was not started"
        assert gate.said_requests()[0].transcript[-1].text == final.text
        gate.go.set()
        await settle(200)
        if not task.done():
            task.cancel()
    assert len(gate.results) == 1 and gate.results[0].intent == m.SAID_OK
