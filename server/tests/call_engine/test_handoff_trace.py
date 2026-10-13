"""AC-75：转接期间 turns 只新增 handoff_started、handoff_ended 两类事件，没有 transcript 行。

机制（tech §7.1 AC-75、A-12 定稿）：TranscriptTap 订阅 SonicTap 的 provider 文字事件；media_gate.in_handoff 时
一律丢弃，不转给 TurnDetector / LiveActionBackend，也不写 trace。转接外：USER FINAL 交 TurnDetector；
ASSISTANT FINAL 写 waitless_utterance 并交 LiveActionBackend.on_assistant_final；SPECULATIVE 不写 trace。
缺口 D-G-4（定稿 ready_before_started）：转接事件固定顺序 handoff_announced → handoff_ready → handoff_started →
handoff_ended，所以 handoff_started 与 handoff_ended 之间没有任何别的事件。
Jev 定稿 no_trace_in_handoff：转接中 Sonic 自己出声，OutputGuard 丢弃它但不写 trace（不写 guard_blocked），只在内存计数；
handoff_ended 的 payload（TraceEvent.action）带 dropped_sonic_outputs = 丢弃的 Sonic 回应段数。
"""

from typing import Any

import pytest

from call_engine._support import (
    HANDOFF_EVENT_ORDER,
    dropped_counts,
    FakeActions,
    FakeMediaGate,
    FakeTurnDetector,
    LiveCallHarness,
    first_seq,
    handoff_sonic,
    join_handoff,
    patch_menu_parser,
    reach_handoff_ready,
    window,
)
from fakes import SonicResponse
from waitless import models as m


def ev(role: str, stage: str, text: str, clock: Any) -> m.ProviderTextEvent:
    return m.ProviderTextEvent(role=role, stage=stage, text=text, stop_reason="END_TURN", t_ms=clock.monotonic_ms())


def make_tap(deps: Any, spec: Any) -> tuple[Any, FakeTurnDetector, FakeActions, FakeMediaGate]:
    from waitless.call.live import TranscriptTap

    td, actions, gate = FakeTurnDetector(), FakeActions(), FakeMediaGate()
    return TranscriptTap(deps=deps, spec=spec, turn_detector=td, actions=actions, media_gate=gate), td, actions, gate


async def test_ac75_tap_routes_final_text_outside_a_handoff(deps: Any, spec: Any, trace: Any, fake_clock: Any) -> None:
    tap, td, actions, _ = make_tap(deps, spec)
    user = ev("USER", "FINAL", "What is the service address?", fake_clock)
    said = ev("ASSISTANT", "FINAL", "The service address is 1200 W Example St.", fake_clock)
    await tap.on_provider_text(ev("ASSISTANT", "SPECULATIVE", "The service address", fake_clock))
    await tap.on_provider_text(user)
    await tap.on_provider_text(said)
    assert [c[1][0] for c in td.calls if c[0] == "on_provider_text" and c[1][0].stage == "FINAL"] == [user]
    assert [c[1][0] for c in actions.calls if c[0] == "on_assistant_final"] == [said]
    utterances = trace.of_type("waitless_utterance", call_id=spec.call_id)
    assert [(u.speaker, u.transcript) for u in utterances] == [("waitless", said.text)]
    assert all(e.transcript != "The service address" for e in trace.events())  # SPECULATIVE 不写 trace


async def test_ac75_tap_drops_everything_during_a_handoff(deps: Any, spec: Any, trace: Any, fake_clock: Any, decoy_sensitive: dict[str, str]) -> None:
    tap, td, actions, gate = make_tap(deps, spec)
    gate.in_handoff = True
    before = len(trace.events())
    for e in (
        ev("USER", "FINAL", f"My date of birth is {decoy_sensitive['date_of_birth']}.", fake_clock),
        ev("ASSISTANT", "SPECULATIVE", "Thank you.", fake_clock),
        ev("ASSISTANT", "FINAL", "Thank you.", fake_clock),
    ):
        await tap.on_provider_text(e)
    assert td.calls == [] and actions.calls == []
    assert len(trace.events()) == before
    assert decoy_sensitive["date_of_birth"] not in trace.text_dump()


async def test_ac75_live_call_writes_no_transcript_during_the_handoff(
    monkeypatch: pytest.MonkeyPatch, settings: Any, spec: Any, fake_clock: Any, trace: Any, gate: Any,
    make_fake_sonic: Any, fake_carrier: Any, store: Any, engine: Any, decoy_sensitive: dict[str, str],
) -> None:
    patch_menu_parser(monkeypatch)
    h = LiveCallHarness(monkeypatch=monkeypatch, settings=settings, spec=spec, clock=fake_clock, trace=trace,
                        gate=gate, sonic=handoff_sonic(make_fake_sonic), carrier=fake_carrier, store=store)
    try:
        await reach_handoff_ready(h, engine)
        join, ws = await join_handoff(h)
        # 用户亲口报出生日期；即使 Sonic 收到了什么，也不能变成转写或 trace
        await h.speech(1000)
        await h.sonic.emit_user_text(f"It's {decoy_sensitive['date_of_birth']}.")
        await h.silence(engine.turns.turn_end_silence_ms + 300)
        assert h.call.status().transcript_tail == []
        await h.call.return_to_agent(join.handoff_id)
        await h.silence(200)
    finally:
        await h.close()
    inside = window(trace, spec.call_id, "handoff_started", "handoff_ended")
    assert inside == [], [e.event_type for e in inside]
    order = [first_seq(trace, spec.call_id, t) for t in HANDOFF_EVENT_ORDER]
    assert order == sorted(order) and len(set(order)) == len(order), dict(zip(HANDOFF_EVENT_ORDER, order))
    assert decoy_sensitive["date_of_birth"] not in trace.text_dump()
    assert len(gate.round_calls) == 1  # 转接期间没有产出新的一轮


async def test_ac75_sonic_speaking_during_the_handoff_is_dropped_without_trace(
    monkeypatch: pytest.MonkeyPatch, settings: Any, spec: Any, fake_clock: Any, trace: Any, gate: Any,
    make_fake_sonic: Any, fake_carrier: Any, store: Any, engine: Any,
) -> None:
    """转接中 Sonic 自己说了两段：一帧都不上线路，转接窗口里仍然没有任何事件（不写 guard_blocked），
    handoff_ended 的 dropped_sonic_outputs 是 2。"""
    patch_menu_parser(monkeypatch)
    h = LiveCallHarness(monkeypatch=monkeypatch, settings=settings, spec=spec, clock=fake_clock, trace=trace,
                        gate=gate, sonic=handoff_sonic(make_fake_sonic), carrier=fake_carrier, store=store)
    try:
        await reach_handoff_ready(h, engine)
        join, _ = await join_handoff(h)
        audio_before = len(h.transport.output().tts_audio())
        await h.speech(400)
        await h.sonic_says(SonicResponse(text="One moment please.", audio_ms=200, chunk_ms=40))
        await h.silence(300)
        await h.sonic_says(SonicResponse(text="Is there anything else I can help with?", audio_ms=400, chunk_ms=40))
        await h.silence(300)
        audio_during = len(h.transport.output().tts_audio())
        await h.call.return_to_agent(join.handoff_id)
        await h.silence(200)
    finally:
        await h.close()
    assert audio_during == audio_before, "Sonic audio reached the line during the handoff"
    assert window(trace, spec.call_id, "handoff_started", "handoff_ended") == []
    assert dropped_counts(trace, spec.call_id) == [2]
