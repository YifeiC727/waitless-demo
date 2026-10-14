"""D-F2: executor silence reaches the line without creating a Sonic response."""
import pytest
from pipecat.frames.frames import OutputAudioRawFrame, OutputDTMFUrgentFrame
from call_engine.conftest import compiled, engine, gate, line_number, settings, spec, store, trace
from call_engine._support import LiveCallHarness, audio_ms, make_result, record_decision, settle


@pytest.mark.parametrize("phase,digits", [("ivr_menu", "1"), ("live_rep", "12#")])
async def test_dtmf_tail_reaches_line_without_guard_events(phase, digits, monkeypatch, settings, spec,
        fake_clock, trace, gate, make_fake_sonic, fake_carrier, store, engine):
    h = LiveCallHarness(monkeypatch=monkeypatch, settings=settings, spec=spec, clock=fake_clock,
        trace=trace, gate=gate, sonic=make_fake_sonic(), carrier=fake_carrier, store=store)
    try:
        await h.start()
        await h.connect_line()
        result = record_decision(trace, make_result("press", call_phase=phase, digits=digits),
                                 task_id=spec.task_id, call_id=spec.call_id, turn_seq=0)
        await h.call._actions.execute(result)
        await settle()
        out = h.transport.output()
        frames = [f for f in out.frames if isinstance(f, (OutputDTMFUrgentFrame, OutputAudioRawFrame))]
        assert "".join(f.to_string() for f in frames if isinstance(f, OutputDTMFUrgentFrame)) == digits
        audio = [f for f in frames if isinstance(f, OutputAudioRawFrame)]
        assert audio_ms(audio) == engine.dtmf.tail_silence_ms
        assert all(not any(f.audio) for f in audio)
        assert isinstance(frames[-1], OutputAudioRawFrame)
        guard = h.call._guard
        assert not guard._responses and guard.counters.blocked == guard.counters.released == 0
        assert guard._last_audio_ms is None
        await fake_clock.advance(engine.guard.held_response_max_ms + 100)
        assert not trace.of_type("guard_blocked", call_id=spec.call_id)
        assert not trace.of_type("guard_released", call_id=spec.call_id)
        await h.call._actions.execute(result)
        await settle()
        assert [f for f in out.frames if isinstance(f, (OutputDTMFUrgentFrame, OutputAudioRawFrame))] == frames
        # MediaGateOut must continue to suppress the same executor frames during handoff.
        await h.call._media_gate.enter_handoff()
        other = record_decision(trace, make_result("press", call_phase=phase, digits=digits),
                                task_id=spec.task_id, call_id=spec.call_id, turn_seq=1)
        await h.call._actions.execute(other)
        await settle()
        assert [f for f in out.frames if isinstance(f, (OutputDTMFUrgentFrame, OutputAudioRawFrame))] == frames
    finally:
        await h.close()
