"""AC-78：用户调用 return_to_agent，或转接 WebSocket 断开满 3 秒（engine.handoff.resume_grace_ms）后，程序注入
「验证完成，继续任务」，状态变为 resuming。

机制（tech §7.1 AC-78、§6.4；缺口 E-G-5、D-G-5 定稿）：LiveCall.return_to_agent → MediaGate.exit_handoff →
写 handoff_ended → 注入 say_templates["handoff.resume"]（resume_injected）→ phase=resuming；浏览器收到
{"type":"ended","reason":"returned"}。用户接入期间 phase=in_handoff。WebSocket 不经 return 断开：handoff_endpoint
看 phase 仍是 in_handoff，就等 resume_grace_ms，到时仍是 in_handoff 才调 return_to_agent；用户在等待期间点了
Return（返回 no_active_handoff）就当已接回，不会接回两次。
"""

from typing import Any

import pytest

from call_engine._support import (
    SAY_TEMPLATES,
    LiveCallHarness,
    handoff_sonic,
    join_handoff,
    patch_menu_parser,
    reach_handoff_ready,
    settle,
    wait_until,
)
from waitless import models as m

RESUME = m.SONIC_INSTRUCTION_PREFIX + SAY_TEMPLATES["handoff.resume"]


def harness(monkeypatch: Any, settings: Any, spec: Any, fake_clock: Any, trace: Any, gate: Any,
            make_fake_sonic: Any, fake_carrier: Any, store: Any) -> LiveCallHarness:
    patch_menu_parser(monkeypatch)
    return LiveCallHarness(monkeypatch=monkeypatch, settings=settings, spec=spec, clock=fake_clock, trace=trace,
                           gate=gate, sonic=handoff_sonic(make_fake_sonic), carrier=fake_carrier, store=store)


async def test_ac78_return_to_agent_resumes_the_call(
    monkeypatch: pytest.MonkeyPatch, settings: Any, spec: Any, fake_clock: Any, trace: Any, gate: Any,
    make_fake_sonic: Any, fake_carrier: Any, store: Any, engine: Any,
) -> None:
    h = harness(monkeypatch, settings, spec, fake_clock, trace, gate, make_fake_sonic, fake_carrier, store)
    try:
        await reach_handoff_ready(h, engine)
        join, ws = await join_handoff(h)
        assert h.call.status().phase == "in_handoff"
        assert RESUME not in h.sonic.sent_texts
        nonzero_before = h.sonic.input_frames - h.sonic.input_zero_frames
        out = await h.call.return_to_agent(join.handoff_id)
        await settle(200)
        phase = h.call.status().phase
        await wait_until(lambda: ws.closed, what="the handoff socket to close")
        await h.speech(200)
        nonzero_after = h.sonic.input_frames - h.sonic.input_zero_frames
    finally:
        await h.close()
    assert out == m.ReturnOut(
        task_id=spec.task_id, call_id=spec.call_id, status="resuming",
        media=m.ReturnMedia(ai_media="connected", recording=False, transcription=True),
    )
    assert phase == "resuming"
    assert h.sonic.sent_texts.count(RESUME) == 1
    assert {"type": "ended", "reason": "returned"} in ws.sent_json()
    types = trace.types(spec.call_id)
    assert types.index("handoff_ended") < types.index("resume_injected")
    resume = [e for e in trace.of_type("injection_sent", call_id=spec.call_id) if (e.action or {}).get("template") == "handoff.resume"]
    assert len(resume) == 1 and trace.has_decision(resume[0].decision_id)
    assert nonzero_after > nonzero_before, "the company's audio does not reach Sonic again after resuming"
    assert trace.violations == []


async def test_ac78_socket_drop_without_return_resumes_after_the_grace_period(
    monkeypatch: pytest.MonkeyPatch, settings: Any, spec: Any, fake_clock: Any, trace: Any, gate: Any,
    make_fake_sonic: Any, fake_carrier: Any, store: Any, engine: Any,
) -> None:
    grace = engine.handoff.resume_grace_ms
    h = harness(monkeypatch, settings, spec, fake_clock, trace, gate, make_fake_sonic, fake_carrier, store)
    try:
        await reach_handoff_ready(h, engine)
        _, ws = await join_handoff(h)
        ws.disconnect(1006)  # 浏览器断线，没有点 Return
        await settle(200)
        await h.silence(grace - 500)
        assert RESUME not in h.sonic.sent_texts, "resumed before resume_grace_ms"
        assert h.call.status().phase == "in_handoff"
        await h.silence(1000)
        phase = h.call.status().phase
    finally:
        await h.close()
    assert h.sonic.sent_texts.count(RESUME) == 1
    assert phase == "resuming"
    types = trace.types(spec.call_id)
    assert "handoff_ended" in types and types.index("handoff_ended") < types.index("resume_injected")


async def test_ac78_return_during_the_grace_wait_resumes_only_once(
    monkeypatch: pytest.MonkeyPatch, settings: Any, spec: Any, fake_clock: Any, trace: Any, gate: Any,
    make_fake_sonic: Any, fake_carrier: Any, store: Any, engine: Any,
) -> None:
    """浏览器先断线，用户在 resume_grace_ms 之内又点了 Return：只接回一次，端点到时不再接回。"""
    grace = engine.handoff.resume_grace_ms
    h = harness(monkeypatch, settings, spec, fake_clock, trace, gate, make_fake_sonic, fake_carrier, store)
    try:
        await reach_handoff_ready(h, engine)
        join, ws = await join_handoff(h)
        ws.disconnect(1006)
        await settle(200)
        await h.silence(grace // 2)
        out = await h.call.return_to_agent(join.handoff_id)
        await h.silence(grace + 500)
        phase = h.call.status().phase
    finally:
        await h.close()
    assert out.status == "resuming" and phase == "resuming"
    assert h.sonic.sent_texts.count(RESUME) == 1
    assert len(trace.of_type("resume_injected", call_id=spec.call_id)) == 1
    assert len(trace.of_type("handoff_ended", call_id=spec.call_id)) == 1
