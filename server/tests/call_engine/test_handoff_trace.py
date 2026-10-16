"""AC-75: handoff excludes transcripts and Sonic audio; only tool telemetry may finish in the private window."""

from typing import Any

import pytest

from call_engine._support import (
    HANDOFF_EVENT_ORDER,
    dropped_counts,
    FakeActions,
    FakeMediaGate,
    FakeTurnDetector,
    first_seq,
    handoff_sonic,
    join_handoff,
    patch_menu_parser,
    window,
)
from fakes import SonicResponse
from waitless import models as m


from lane_D.agent_support import AgentCallHarness as LiveCallHarness, reach_handoff_ready

def ev(role: str, stage: str, text: str, clock: Any) -> m.ProviderTextEvent:
    return m.ProviderTextEvent(role=role, stage=stage, text=text, stop_reason="END_TURN", t_ms=clock.monotonic_ms())


def make_tap(deps, spec):
    from types import SimpleNamespace
    from waitless.call.live import TranscriptTap
    gate = FakeMediaGate()
    return TranscriptTap(deps=deps, spec=spec, toolbox=SimpleNamespace(phase='live_rep'), media_gate=gate), gate


async def test_ac75_tap_routes_final_text_outside_a_handoff(deps, spec, trace, fake_clock):
    tap, _ = make_tap(deps, spec)
    await tap.on_provider_text(ev('ASSISTANT', 'SPECULATIVE', 'The service address', fake_clock))
    await tap.on_provider_text(ev('USER', 'FINAL', 'What is the service address?', fake_clock))
    await tap.on_provider_text(ev('ASSISTANT', 'FINAL', 'The service address is authorized.', fake_clock))
    assert [e.transcript for e in trace.of_type('company_turn')] == ['What is the service address?']
    assert [e.transcript for e in trace.of_type('waitless_utterance')] == ['The service address is authorized.']


async def test_ac75_tap_drops_everything_during_a_handoff(deps, spec, trace, fake_clock, decoy_sensitive):
    tap, gate = make_tap(deps, spec)
    gate.in_handoff = True
    before = trace.events()
    for role, stage, text in [('USER', 'FINAL', decoy_sensitive['date_of_birth']),
                             ('ASSISTANT', 'SPECULATIVE', 'Thank you.'),
                             ('ASSISTANT', 'FINAL', 'Thank you.')]:
        await tap.on_provider_text(ev(role, stage, text, fake_clock))
    assert trace.events() == before
    assert decoy_sensitive['date_of_birth'] not in trace.text_dump()


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
    assert all(e.event_type == 'agent_tool_result' for e in inside), [e.event_type for e in inside]
    order = [first_seq(trace, spec.call_id, t) for t in HANDOFF_EVENT_ORDER]
    assert order == sorted(order) and len(set(order)) == len(order), dict(zip(HANDOFF_EVENT_ORDER, order))
    assert decoy_sensitive["date_of_birth"] not in trace.text_dump()
    assert len(gate.round_calls) == 0  # 转接期间没有产出新的一轮


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
    assert all(e.event_type == 'agent_tool_result' for e in window(trace, spec.call_id, 'handoff_started', 'handoff_ended'))
    assert dropped_counts(trace, spec.call_id) == [2]
