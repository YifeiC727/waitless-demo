"""I2B-1: speech turns wait for the provider FINAL to catch up with audio."""
import pytest

from call_engine.conftest import engine
from call_engine._support import (
    MENU_PATTERNS, STEP_MS, Chain, in_frame, patch_menu_parser, tone_pcm16, zero_pcm16,
)
from waitless.audio.vad import EnergyVad
from waitless.call.turn_detector import TurnDetector
from waitless.models import ProviderTextEvent


async def replay(engine, clock, monkeypatch, voices, finals, until):
    patch_menu_parser(monkeypatch)
    emitted = []

    async def on_turn(turn):
        emitted.append((clock.monotonic_ms(), turn))

    rate = engine.audio.pipeline_in_sample_rate
    detector = TurnDetector(call_id="call-d-r8", clock=clock, turns=engine.turns,
        vad=EnergyVad(threshold_dbfs=engine.vad.threshold_dbfs,
            frame_ms=engine.vad.frame_ms, sample_rate=rate),
        menu_patterns=MENU_PATTERNS, on_turn=on_turn)
    # Keep exact event timestamps, including the last voiced frame, alongside 20 ms audio.
    stamps = set(range(STEP_MS, until + 1, STEP_MS)) | set(finals)
    stamps.update(t for interval in voices for t in interval)
    tone, zero = tone_pcm16(STEP_MS, rate), zero_pcm16(STEP_MS, rate)
    async with Chain(detector) as chain:
        for stamp in sorted(stamps):
            await clock.advance(stamp - clock.monotonic_ms())
            await chain.push(in_frame(tone if any(a <= stamp <= b for a, b in voices) else zero, rate))
            if stamp in finals:
                await detector.on_provider_text(ProviderTextEvent(role="USER", stage="FINAL",
                    text=finals[stamp], stop_reason="PARTIAL_TURN", t_ms=stamp))
    return emitted


async def test_i2b1_live_timeline_keeps_the_850ms_pause_in_one_turn(engine, fake_clock, monkeypatch):
    emitted = await replay(engine, fake_clock, monkeypatch,
        [(600, 5658), (6508, 11608)], {
            3943: "thanks for calling to proceed i need to collect a couple",
            7923: "of details and verify your account first could you please",
            12023: "confirm the full service address and the desired start date for the electric",
        }, until=13620)
    assert len(emitted) == 1, [(at, turn.text) for at, turn in emitted]
    at, turn = emitted[0]
    assert turn.kind == "speech"
    assert "a couple of details" in turn.text and "service address" in turn.text
    assert turn.t_start_ms == 600 and turn.t_end_ms == 11608
    deadline = 11608 + engine.turns.turn_end_silence_ms
    assert deadline <= at < deadline + STEP_MS


async def test_i2b1_1400ms_pause_uses_the_grace_fallback(engine, fake_clock, monkeypatch):
    emitted = await replay(engine, fake_clock, monkeypatch,
        [(600, 5658), (7058, 12158)], {
            3943: "thanks for calling to proceed i need to collect a couple",
            8473: "of details and verify your account first could you please",
            12573: "confirm the full service address",
        }, until=14200)
    assert len(emitted) == 2
    at, turn = emitted[0]
    deadline = 5658 + engine.turns.turn_end_silence_ms + engine.turns.transcript_grace_ms
    assert deadline <= at < deadline + STEP_MS
    assert turn.t_end_ms == 5658


@pytest.mark.parametrize("final_at", [800, 1000, 1300, 1800])
async def test_i2b1_final_catchup_and_strict_boundary(engine, fake_clock, monkeypatch, final_at):
    emitted = await replay(engine, fake_clock, monkeypatch, [(200, 1000)],
        {final_at: "What is the service address?"}, until=2600)
    assert len(emitted) == 1
    caught_up = final_at > 1000
    deadline = 1000 + engine.turns.turn_end_silence_ms
    if not caught_up:
        deadline += engine.turns.transcript_grace_ms
    else:
        deadline = max(deadline, final_at)
    assert deadline <= emitted[0][0] < deadline + STEP_MS
    assert emitted[0][1].t_end_ms == 1000


async def test_i2b1_audio_without_final_does_not_emit(engine, fake_clock, monkeypatch):
    assert await replay(engine, fake_clock, monkeypatch, [(200, 1000)], {}, until=4000) == []


@pytest.mark.parametrize("final_at", [800, 1300, 2300])
async def test_i2b1_menu_silence_and_final_grace_unchanged(engine, fake_clock, monkeypatch, final_at):
    emitted = await replay(engine, fake_clock, monkeypatch, [(200, 1000)],
        {final_at: "For billing, press 1. To start service, press 2."}, until=4000)
    assert len(emitted) == 1
    deadline = max(1000 + engine.turns.menu_end_silence_ms,
        final_at + engine.turns.transcript_grace_ms)
    assert deadline <= emitted[0][0] < deadline + STEP_MS
    assert emitted[0][1].kind == "menu"
