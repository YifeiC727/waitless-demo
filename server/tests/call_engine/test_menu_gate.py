"""VAD and standalone transcript timing remain covered; live keypad actions come only from Jev."""

from types import SimpleNamespace
from typing import Any

import pytest

from call_engine._support import (
    MENU_PATTERNS,
    STEP_MS,
    Chain,
    FakeCarrierApi,
    FakeGuard,
    FakeInjector,
    FakeMediaGate,
    FakePipelineTask,
    LiveCallHarness,
    TEST_CALL_SID,
    drive,
    dtmf_digits,
    in_frame,
    make_result,
    patch_menu_parser,
    record_decision,
    tone_pcm16,
    zero_pcm16,
)
from fakes import SonicResponse
from waitless import models as m

MENU_TEXT = "Thanks for calling Lakeshore Energy. For billing, press 1. To start or stop service, press 2."
SPEECH_TEXT = "Thanks for holding. What is the service address?"


# ---------------------------------------------------------------------------
# 能量 VAD（audio/vad.py，参数取 engine.yaml 的 vad 段）
# ---------------------------------------------------------------------------


def make_vad(engine: Any) -> Any:
    from waitless.audio.vad import EnergyVad

    return EnergyVad(
        threshold_dbfs=engine.vad.threshold_dbfs,
        frame_ms=engine.vad.frame_ms,
        sample_rate=engine.audio.pipeline_in_sample_rate,
    )


def test_ac66_vad_separates_speech_from_silence(engine: Any) -> None:
    vad = make_vad(engine)
    rate = engine.audio.pipeline_in_sample_rate
    frame = engine.vad.frame_ms
    assert vad.is_speech(tone_pcm16(frame, rate, amplitude=0.1))
    assert not vad.is_speech(zero_pcm16(frame, rate))
    assert not vad.is_speech(tone_pcm16(frame, rate, amplitude=0.0005))  # 约 −69 dBFS，低于阈值
    assert vad.level_dbfs(zero_pcm16(frame, rate)) == float("-inf")
    assert vad.level_dbfs(tone_pcm16(frame, rate, amplitude=0.1)) == pytest.approx(-23.0, abs=0.5)


def test_ac66_vad_feed_tracks_last_voice(engine: Any) -> None:
    vad = make_vad(engine)
    rate = engine.audio.pipeline_in_sample_rate
    frame = engine.vad.frame_ms
    assert vad.last_voice_ms is None
    assert vad.feed(zero_pcm16(3 * frame, rate), 0) is False
    assert vad.last_voice_ms is None
    assert vad.feed(zero_pcm16(frame, rate) + tone_pcm16(frame, rate), 1000) is True
    first = vad.last_voice_ms
    assert first is not None and 1000 <= first <= 1000 + 2 * frame
    assert vad.feed(zero_pcm16(2 * frame, rate), 2000) is False
    assert vad.last_voice_ms == first


# ---------------------------------------------------------------------------
# TurnDetector
# ---------------------------------------------------------------------------


@pytest.fixture
def detector(engine: Any, fake_clock: Any, monkeypatch: pytest.MonkeyPatch) -> SimpleNamespace:
    from waitless.call.turn_detector import TurnDetector

    parse_calls = patch_menu_parser(monkeypatch)
    turns: list[m.CompanyTurn] = []

    async def on_turn(turn: m.CompanyTurn) -> None:
        turns.append(turn)

    det = TurnDetector(
        call_id="call-d-1",
        vad=make_vad(engine),
        turns=engine.turns,
        menu_patterns=MENU_PATTERNS,
        on_turn=on_turn,
        clock=fake_clock,
    )
    return SimpleNamespace(det=det, turns=turns, parse_calls=parse_calls, chain=Chain(det))


def user_final(text: str, clock: Any, stop_reason: str = "END_TURN") -> m.ProviderTextEvent:
    return m.ProviderTextEvent(role="USER", stage="FINAL", text=text, stop_reason=stop_reason, t_ms=clock.monotonic_ms())


async def play(env: SimpleNamespace, clock: Any, engine: Any, *, speech_ms: int, silence_ms: int,
               finals: dict[int, tuple[str, str]], before_speech_ms: int = 0) -> dict[int, int]:
    """送 before_speech_ms 静音、speech_ms 有声、silence_ms 静音；静音到 finals 的某个毫秒时发对应 USER FINAL。
    返回 {静音毫秒: 当时已产出的轮数}。"""
    rate = engine.audio.pipeline_in_sample_rate
    for _ in range(before_speech_ms // STEP_MS):
        await clock.advance(STEP_MS)
        await env.chain.push(in_frame(zero_pcm16(STEP_MS, rate), rate))
    for _ in range(speech_ms // STEP_MS):
        await clock.advance(STEP_MS)
        await env.chain.push(in_frame(tone_pcm16(STEP_MS, rate), rate))
    seen: dict[int, int] = {}
    elapsed = 0
    while elapsed < silence_ms:
        await clock.advance(STEP_MS)
        await env.chain.push(in_frame(zero_pcm16(STEP_MS, rate), rate))
        elapsed += STEP_MS
        if elapsed in finals:
            text, stop = finals[elapsed]
            await env.det.on_provider_text(user_final(text, clock, stop))
        seen[elapsed] = len(env.turns)
    return seen


def first_turn_at(seen: dict[int, int]) -> int | None:
    for ms in sorted(seen):
        if seen[ms]:
            return ms
    return None


async def test_ac66_menu_turn_waits_for_menu_end_silence(detector: SimpleNamespace, fake_clock: Any, engine: Any) -> None:
    menu_end = engine.turns.menu_end_silence_ms
    async with detector.chain:
        seen = await play(detector, fake_clock, engine, speech_ms=1500, silence_ms=menu_end + 600,
                          finals={300: (MENU_TEXT, "PARTIAL_TURN")})
    at = first_turn_at(seen)
    assert at is not None, "no CompanyTurn after the menu ended"
    assert at >= menu_end - STEP_MS, f"menu turn produced after only {at} ms of silence (menu_end_silence_ms={menu_end})"
    assert at <= menu_end + 3 * STEP_MS
    assert len(detector.turns) == 1
    turn = detector.turns[0]
    assert turn.kind == "menu" and turn.call_id == "call-d-1"
    assert {"1", "2"} <= {o.digits for o in turn.menu_options}
    assert turn.text.strip() == MENU_TEXT
    assert detector.parse_calls and all(patterns == MENU_PATTERNS for _, patterns, _ in detector.parse_calls)


async def test_ac66_speech_turn_after_turn_end_silence(detector: SimpleNamespace, fake_clock: Any, engine: Any) -> None:
    turn_end = engine.turns.turn_end_silence_ms
    async with detector.chain:
        seen = await play(detector, fake_clock, engine, speech_ms=1000, silence_ms=turn_end + 600,
                          finals={300: (SPEECH_TEXT, "END_TURN")})
    at = first_turn_at(seen)
    assert at is not None
    assert turn_end - STEP_MS <= at <= turn_end + 3 * STEP_MS
    assert [t.kind for t in detector.turns] == ["speech"]
    assert detector.turns[0].menu_options == []


async def test_ac66_turn_times_mark_the_company_speech(detector: SimpleNamespace, fake_clock: Any, engine: Any) -> None:
    """t_start_ms/t_end_ms 是对方这句话的起止（A-15 按 t_end_ms 归属回应；A-08 company_turn.t_ms 取 t_end_ms），
    不是轮次产出的时刻。"""
    async with detector.chain:
        await play(detector, fake_clock, engine, before_speech_ms=400, speech_ms=1000,
                   silence_ms=engine.turns.turn_end_silence_ms + 300, finals={300: (SPEECH_TEXT, "END_TURN")})
    turn = detector.turns[0]
    speech_start, speech_end = 400, 1400  # FakeClock 毫秒：静音 400 ms 后说 1000 ms
    assert speech_start - 2 * STEP_MS <= turn.t_start_ms <= speech_start + 2 * STEP_MS
    assert speech_end - 2 * STEP_MS <= turn.t_end_ms <= speech_end + 2 * STEP_MS


async def test_ac66_only_user_final_text_makes_turns(detector: SimpleNamespace, fake_clock: Any, engine: Any) -> None:
    async with detector.chain:
        await play(detector, fake_clock, engine, speech_ms=600, silence_ms=200, finals={})
        for ev in (
            m.ProviderTextEvent(role="USER", stage="SPECULATIVE", text=MENU_TEXT, stop_reason="PARTIAL_TURN", t_ms=fake_clock.monotonic_ms()),
            m.ProviderTextEvent(role="ASSISTANT", stage="FINAL", text=MENU_TEXT, stop_reason="END_TURN", t_ms=fake_clock.monotonic_ms()),
        ):
            await detector.det.on_provider_text(ev)
        await play(detector, fake_clock, engine, speech_ms=0, silence_ms=engine.turns.menu_end_silence_ms + 1000, finals={})
    assert detector.turns == []


async def test_ac66_turn_detector_passes_audio_through(detector: SimpleNamespace, fake_clock: Any, engine: Any) -> None:
    rate = engine.audio.pipeline_in_sample_rate
    sent = [tone_pcm16(STEP_MS, rate), zero_pcm16(STEP_MS, rate), tone_pcm16(STEP_MS, rate, amplitude=0.2)]
    async with detector.chain:
        for pcm in sent:
            await fake_clock.advance(STEP_MS)
            await detector.chain.push(in_frame(pcm, rate))
    from pipecat.frames.frames import InputAudioRawFrame

    assert [f.audio for f in detector.chain.tail.down(InputAudioRawFrame)] == sent


# ---------------------------------------------------------------------------
# press：每个决定只推一次 DTMF
# ---------------------------------------------------------------------------


from fakes import FakeJev, jev_choice
from lane_D.agent_support import agent_call, settle, engine, settings, compiled, spec, line_number, store, trace


async def test_ac66_press_pushes_exactly_one_dtmf(agent_call):
    h = agent_call
    h.deps.jev = FakeJev([{'keypad': jev_choice('2'), 'speaker': jev_choice('menu_or_recording')}])
    h.component_tools = False
    await h.boot()
    await h.sonic.emit_user_text('For service, press 2.')
    await settle(200)
    assert len(h.transport.output().dtmf()) == 1
    assert dtmf_digits(h.transport.output().dtmf()) == '2'
    assert len(h.events('dtmf_sent')) == 1
    h.assert_trace_contract()


async def test_ac66_live_call_presses_once_after_menu_end(agent_call):
    h = agent_call
    h.deps.jev = FakeJev([{'keypad': jev_choice('2'), 'speaker': jev_choice('menu_or_recording')}])
    h.component_tools = False
    await h.boot()
    h.call._toolbox.phase = 'ivr'
    await h.company_says(MENU_TEXT)
    await settle(200)
    assert dtmf_digits(h.transport.output().dtmf()) == '2'
    sent = h.events('dtmf_sent')[0]
    decision = h.trace.decision(sent.decision_id)
    assert decision.action['turn_seq'] == h.events('company_turn')[0].action['seq']
    assert h.events('company_turn')[0].action['kind'] == 'menu'
    h.assert_trace_contract()
