"""AC-66 菜单把关（修正 S0 V1 的抢按）：对方静音满 menu_end_silence_ms 之前不发按键；之后按 Jev 对完整
选项的 menu_choice 只发一次按键。

机制（tech §6.5、§7.1 AC-66；S0-r2 V12）：
- TurnDetector 用能量 VAD 判静音；文字只来自 provider 的 USER FINAL（PARTIAL_TURN 也算）；
  菜单轮要静音满 turns.menu_end_silence_ms，且最后一块 FINAL 之后再等 turns.transcript_grace_ms。
- LiveActionBackend.execute(press) 经 pipeline_task 推一次 OutputDTMFUrgentFrame.from_string(digits)，写 dtmf_sent。
- A-08：dtmf_sent.decision_id → decision（action.turn_seq）→ company_turn（action.seq，t_ms = CompanyTurn.t_end_ms）。
所有时长取 server/config/engine.yaml；时间用 FakeClock，每 20 ms 送一帧。

TurnDetector 内部要调 G 线的 decision.menu_parse.parse_menu；测试把它换成确定的替身（_support.patch_menu_parser）。
"""

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


def press_backend(spec: Any, deps: Any) -> SimpleNamespace:
    from waitless.call.actions import LiveActionBackend

    env = SimpleNamespace(
        injector=FakeInjector(), guard=FakeGuard(), media_gate=FakeMediaGate(), task=FakePipelineTask(), carrier=FakeCarrierApi()
    )
    env.actions = LiveActionBackend(
        spec=spec, deps=deps, injector=env.injector, output_guard=env.guard, media_gate=env.media_gate,
        pipeline_task=env.task, carrier=env.carrier, call_sid=TEST_CALL_SID,
    )
    return env


async def test_ac66_press_pushes_exactly_one_dtmf(spec: Any, deps: Any, trace: Any, fake_clock: Any) -> None:
    env = press_backend(spec, deps)
    result = record_decision(
        trace, make_result("press", intent="2", digits="2", call_phase="ivr_menu"), task_id=spec.task_id, call_id=spec.call_id, turn_seq=0
    )
    record = await drive(env.actions.execute(result), fake_clock)
    assert (record.decision_id, record.action, record.ok) == (result.decision_id, "press", True)
    assert len(env.task.dtmf()) == 1 and dtmf_digits(env.task.dtmf()) == "2"
    sent = trace.of_type("dtmf_sent", call_id=spec.call_id)
    assert len(sent) == 1 and sent[0].decision_id == result.decision_id
    assert (sent[0].action or {}).get("digits") == "2"
    assert trace.violations == []
    # 同一个决定再执行一次也不会再按（AC-66：只发一次）
    try:
        await drive(env.actions.execute(result), fake_clock)
    except NotImplementedError:
        raise
    except Exception:  # noqa: BLE001 - 重复执行可以报错，也可以忽略；只要求不再按键
        pass
    assert len(env.task.dtmf()) == 1
    assert len(trace.of_type("dtmf_sent", call_id=spec.call_id)) == 1


# ---------------------------------------------------------------------------
# 端到端：LiveCall 里菜单播完才按，只按一次
# ---------------------------------------------------------------------------


async def test_ac66_live_call_presses_once_after_menu_end(
    monkeypatch: pytest.MonkeyPatch, settings: Any, spec: Any, fake_clock: Any, trace: Any, gate: Any,
    make_fake_sonic: Any, fake_carrier: Any, store: Any, engine: Any,
) -> None:
    parse_calls = patch_menu_parser(monkeypatch)
    gate.round_script.append(make_result("press", intent="2", digits="2", call_phase="ivr_menu"))
    sonic = make_fake_sonic()
    h = LiveCallHarness(monkeypatch=monkeypatch, settings=settings, spec=spec, clock=fake_clock, trace=trace,
                        gate=gate, sonic=sonic, carrier=fake_carrier, store=store)
    menu_end = engine.turns.menu_end_silence_ms
    dtmf_by_ms: dict[int, int] = {}
    try:
        await h.start()
        await h.connect_line()
        await h.speech(600)
        await h.sonic_says(SonicResponse(text="Pressing one now.", audio_ms=400))  # S0 V1：Sonic 抢着说按键
        await h.company_says(MENU_TEXT, speech_ms=900, final_after_ms=300, silence_ms=menu_end + 600,
                             stop_reason="PARTIAL_TURN",
                             on_frame=lambda ms: dtmf_by_ms.__setitem__(ms, len(h.transport.output().dtmf())))
    finally:
        await h.close()
    pressed_at = min((ms for ms, n in dtmf_by_ms.items() if n), default=None)
    assert pressed_at is not None, "no DTMF reached the line after the menu"
    assert pressed_at >= menu_end - 2 * STEP_MS, f"pressed after only {pressed_at} ms of silence"
    assert dtmf_digits(h.transport.output().dtmf()) == "2"
    assert len(h.transport.output().dtmf()) == 1
    assert h.transport.output().tts_audio() == []  # 菜单期间 Sonic 的话不进线路（AC-67）
    # E-G-4：菜单判断用的正则与数字词来自 CompiledCall
    assert parse_calls and {(p, tuple(sorted((w or {}).items()))) for _, p, w in parse_calls} == {
        (tuple(spec.compiled.press_patterns), tuple(sorted(spec.compiled.digit_words.items())))
    }
    assert len(gate.round_calls) == 1
    turn = gate.round_calls[0][0]
    assert turn.kind == "menu" and {"1", "2"} <= {o.digits for o in turn.menu_options}
    # A-08 追溯链：dtmf_sent → decision(turn_seq) → company_turn
    (sent,) = trace.of_type("dtmf_sent", call_id=spec.call_id)
    decision = trace.decision(sent.decision_id)
    assert decision is not None and (decision.action or {}).get("turn_seq") == turn.seq
    company = [e for e in trace.of_type("company_turn", call_id=spec.call_id) if (e.action or {}).get("seq") == turn.seq]
    assert len(company) == 1
    assert company[0].t_ms == turn.t_end_ms and (company[0].action or {}).get("kind") == "menu"
    assert trace.violations == []
