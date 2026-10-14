"""D-s5: measure menu silence from the last voiced subframe."""

from call_engine.conftest import engine
from call_engine._support import in_frame, tone_pcm16, zero_pcm16
from call_engine.test_menu_gate import MENU_TEXT, detector, user_final


async def test_batched_audio_does_not_shorten_menu_silence(detector, fake_clock, engine):
    rate = engine.audio.pipeline_in_sample_rate
    step = engine.vad.frame_ms
    batch = zero_pcm16(step, rate) + tone_pcm16(4 * step, rate)
    last_voice = 4 * step
    async with detector.chain:
        await detector.chain.push(in_frame(batch, rate))
        await fake_clock.advance(5 * step)
        await detector.det.on_provider_text(user_final(MENU_TEXT, fake_clock))
        await fake_clock.advance(engine.turns.menu_end_silence_ms - 5 * step)
        await detector.chain.push(in_frame(zero_pcm16(step, rate), rate))
        assert detector.turns == [], "the last voiced subframe has not had a full menu silence"
        await fake_clock.advance(last_voice)
        await detector.chain.push(in_frame(zero_pcm16(step, rate), rate))
    assert len(detector.turns) == 1
    assert detector.turns[0].kind == "menu"
    assert detector.turns[0].t_end_ms == last_voice
