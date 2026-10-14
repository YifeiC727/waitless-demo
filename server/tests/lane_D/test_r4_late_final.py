"""D-F4: a USER FINAL arriving after emission owns a separate turn."""
import pytest
from call_engine.conftest import engine
from call_engine._support import in_frame, tone_pcm16, zero_pcm16
from call_engine.test_menu_gate import detector, user_final, MENU_TEXT


@pytest.mark.parametrize("late_text", ["A late sentence.", MENU_TEXT])
async def test_late_final_emits_during_silence_without_polluting_next_turn(detector, fake_clock, engine, late_text):
    rate, step = engine.audio.pipeline_in_sample_rate, engine.vad.frame_ms
    async with detector.chain:
        await detector.chain.push(in_frame(tone_pcm16(500, rate), rate))
        await fake_clock.advance(500)
        await detector.det.on_provider_text(user_final("First sentence.", fake_clock))
        await fake_clock.advance(max(engine.turns.turn_end_silence_ms, engine.turns.menu_end_silence_ms))
        await detector.chain.push(in_frame(zero_pcm16(step, rate), rate))
        assert len(detector.turns) == 1
        await fake_clock.advance(step)
        arrived = fake_clock.monotonic_ms()
        await detector.det.on_provider_text(user_final(late_text, fake_clock))
        if late_text == MENU_TEXT:
            assert len(detector.turns) == 1
            await fake_clock.advance(engine.turns.transcript_grace_ms - 1)
            await detector.chain.push(in_frame(zero_pcm16(step, rate), rate))
            assert len(detector.turns) == 1
            await fake_clock.advance(1)
            await detector.chain.push(in_frame(zero_pcm16(step, rate), rate))
        assert len(detector.turns) == 2
        first, late = detector.turns
        assert late.text == late_text
        assert late.t_start_ms == late.t_end_ms == arrived
        assert late.t_start_ms > first.t_end_ms
        await fake_clock.advance(step)
        await detector.chain.push(in_frame(tone_pcm16(500, rate), rate))
        await fake_clock.advance(500)
        await detector.det.on_provider_text(user_final("Third sentence.", fake_clock))
        await fake_clock.advance(engine.turns.turn_end_silence_ms)
        await detector.chain.push(in_frame(zero_pcm16(step, rate), rate))
        assert [t.text for t in detector.turns] == ["First sentence.", late_text, "Third sentence."]
