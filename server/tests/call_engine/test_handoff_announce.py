"""AC-77：AI 先对坐席说明——trace 中 handoff_announced（注入的说明话术，以及 Sonic 说完的记录）排在
handoff_started 之前。

机制（tech §7.1 AC-77；缺口 D-G-3、D-G-4、D-G-11 定稿）：策略结果为 handoff 时，LiveActionBackend.execute() 先
inject(say_templates["handoff.announce"]) 并写 handoff_announced；然后等两件事都发生：TranscriptTap 收到这段回应的
ASSISTANT FINAL；从 OutputGuard 放行最后一帧音频起已过 engine.handoff.announce_idle_ms。之后写 handoff_ready、
phase=handoff_ready，再 MediaGate.enter_handoff()，并由 LiveActionBackend 写带这次决策 decision_id 的 handoff_started。
未到 handoff_ready 时 join() 抛 ToolFailure("handoff_not_ready")；那之前的 phase 按 models.CALL_PHASE_FROM_JEV
取最近一轮的 call_phase。
"""

from typing import Any

import pytest

from call_engine._support import (
    ASK_DOB_TEXT,
    COMPANY_NAME,
    SAY_TEMPLATES,
    LiveCallHarness,
    MiniCall,
    first_seq,
    handoff_path,
    handoff_round,
    handoff_sonic,
    patch_menu_parser,
    record_decision,
    response_audio,
    audio_bytes,
    wait_until,
)
from fakes import SonicResponse
from waitless import models as m
from waitless.interfaces import ToolFailure

BEFORE_HANDOFF = m.CALL_PHASE_FROM_JEV[handoff_round().call_phase]  # 坐席要验证身份那一轮的 call_phase（D-G-11）


async def test_ac77_announcement_is_spoken_before_the_handoff_starts(
    monkeypatch: pytest.MonkeyPatch, settings: Any, spec: Any, fake_clock: Any, trace: Any, gate: Any,
    make_fake_sonic: Any, fake_carrier: Any, store: Any, engine: Any,
) -> None:
    patch_menu_parser(monkeypatch)
    sonic = handoff_sonic(make_fake_sonic)
    h = LiveCallHarness(monkeypatch=monkeypatch, settings=settings, spec=spec, clock=fake_clock, trace=trace,
                        gate=gate, sonic=sonic, carrier=fake_carrier, store=store)
    idle = engine.handoff.announce_idle_ms
    try:
        gate.round_script.append(handoff_round())
        await h.start()
        await h.connect_line()
        await h.company_says(ASK_DOB_TEXT, speech_ms=1000, final_after_ms=300,
                             silence_ms=engine.turns.turn_end_silence_ms + 200)
        await wait_until(lambda: sonic.injections, what="the announce injection")
        assert sonic.sent_texts == [m.SONIC_INSTRUCTION_PREFIX + SAY_TEMPLATES["handoff.announce"]]

        # Sonic 还没说：不能进入转接
        await h.silence(idle + 400)
        assert h.call.status().phase == BEFORE_HANDOFF
        assert trace.of_type("handoff_started", call_id=spec.call_id) == []
        with pytest.raises(ToolFailure) as info:
            await h.call.join()
        assert info.value.code == "handoff_not_ready"

        # Sonic 说完（音频放行 + ASSISTANT FINAL），但空闲还不满 announce_idle_ms
        await h.sonic_plays_pending()
        await h.silence(idle - 200)
        assert h.call.status().phase == BEFORE_HANDOFF
        assert trace.of_type("handoff_started", call_id=spec.call_id) == []

        await h.silence(400)
        assert h.call.status().phase == "handoff_ready"
        join = await h.call.join()
        assert h.call.status().phase == "handoff_ready"  # 用户还没接入
    finally:
        await h.close()
    assert join.company_name == COMPANY_NAME and join.ws_path == handoff_path(join.handoff_id)
    assert join.media == m.JoinMedia(ai_media="disconnected", recording=False, transcription=False)
    # 说明话术完整进了线路
    announce_audio = response_audio(sonic, 1)
    assert audio_bytes(h.transport.output().tts_audio())[: len(announce_audio)] == announce_audio
    # trace 顺序：injection_sent(announce) < handoff_announced < handoff_ready < handoff_started（D-G-4）
    events = trace.events(call_id=spec.call_id)
    announce = [e for e in events if e.event_type == "injection_sent" and (e.action or {}).get("template") == "handoff.announce"]
    assert announce, "the announce injection was not traced with its template key"
    order = [first_seq(trace, spec.call_id, t) for t in ("handoff_announced", "handoff_ready", "handoff_started")]
    assert announce[0].seq < order[0] < order[1] < order[2]
    started = [e for e in events if e.event_type == "handoff_started"]
    assert started[0].decision_id and trace.has_decision(started[0].decision_id)
    assert trace.violations == []


async def test_ac77_backend_writes_the_handoff_events_in_order(
    spec: Any, deps: Any, engine: Any, make_fake_sonic: Any, trace: Any
) -> None:
    """LiveActionBackend 单独执行 handoff：说明话术 → handoff_announced → 说完 → handoff_ready → enter_handoff →
    handoff_started（带这次决策的 decision_id）；handoff_ended 不归它写（D-G-3）。"""
    announce = SonicResponse(text=SAY_TEMPLATES["handoff.announce"], audio_ms=400, chunk_ms=40)
    async with MiniCall(spec=spec, deps=deps, engine=engine, make_sonic=make_fake_sonic, responses=[announce]) as mc:
        mc.guard.set_state("conversation")
        result = record_decision(trace, handoff_round(), task_id=spec.task_id, call_id=spec.call_id, turn_seq=9)
        await mc.run(result)
    assert mc.sent_injections() == [SAY_TEMPLATES["handoff.announce"]]
    assert "enter_handoff" in mc.media_gate.names()
    order = [first_seq(trace, spec.call_id, t) for t in ("handoff_announced", "handoff_ready", "handoff_started")]
    assert order == sorted(order) and len(set(order)) == 3
    (started,) = trace.of_type("handoff_started", call_id=spec.call_id)
    assert started.decision_id == result.decision_id
    assert trace.of_type("handoff_ended", call_id=spec.call_id) == []
    assert audio_bytes(mc.line_audio()) == response_audio(mc.sonic, 1)  # 说明话术完整放出
    assert trace.violations == []
