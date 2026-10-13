"""AC-67、AC-68、AC-69（以及 AC-71 的「注入后下一段放行」）：OutputGuard 逐段文字闸门（S0-r2 V8 定论）。

- 一段回应 = LLMFullResponseStartFrame … LLMFullResponseEndFrame，按出现顺序编号；音频（TTSAudioRawFrame）
  先扣住，等这段自己的 SPECULATIVE LLMTextFrame 到了再判断；文字不到就一直扣住。
- ivr：全部丢弃（AC-67）。conversation：文字是 compiled.stall_phrases 之一就立即放行（AC-68）；否则等这一轮的
  决定：release(seq) 放出，其他决定（set_state 切到 injected/handoff/ending，或 discard）丢弃、广播 interruption、
  记 guard_blocked（AC-69）；扣住超过 engine.guard.held_response_max_ms 也丢弃。injected：下一段直接放行。
- A-15 定稿：回应按开始时刻归到轮——开始时刻晚于该轮 CompanyTurn.t_end_ms 的第一段回应归这一轮
  （时钟用 deps.clock.monotonic_ms，与 TurnDetector 同源）。轮次经 OutputGuard.on_turn(turn) 登记（骨架 r2）。
- 生命周期帧、InterruptionFrame 等系统帧以及程序的 DTMF 照常传递。
- Jev 定稿 no_trace_in_handoff：handoff 状态下丢弃 Sonic 输出不写 trace（AC-75 的转接窗口为空），只在内存计数。

断言只看 OutputGuard 推给下游/上游的帧、trace 事件与公开的 counters。
"""

from types import SimpleNamespace
from typing import Any

import pytest
from pipecat.frames.frames import EndFrame, InterruptionFrame, LLMTextFrame, OutputDTMFUrgentFrame, StartFrame

from call_engine._support import (
    STALL_PHRASES,
    Chain,
    audio_bytes,
    make_turn,
    register_turn,
    response_audio,
)
from fakes import SonicResponse

DECISION_TEXT = "Your deposit is waived and autopay is now on."  # 不是 stall 说法：Sonic 擅自替用户做了选择


@pytest.fixture
def g(compiled: Any, engine: Any, trace: Any, fake_clock: Any, make_fake_sonic: Any, spec: Any) -> SimpleNamespace:
    from waitless.call.output_guard import OutputGuard

    guard = OutputGuard(
        stall_phrases=compiled.stall_phrases,
        held_response_max_ms=engine.guard.held_response_max_ms,
        trace=trace,
        call_id=spec.call_id,
        task_id=spec.task_id,
        clock=fake_clock,
    )
    chain = Chain(guard)
    sonic = make_fake_sonic(sink=chain.push)
    return SimpleNamespace(guard=guard, chain=chain, sonic=sonic, trace=trace, call_id=spec.call_id)


def line(env: SimpleNamespace) -> list[bytes]:
    return audio_bytes(env.chain.tail.tts_audio())


def blocked(env: SimpleNamespace) -> int:
    return len(env.trace.of_type("guard_blocked", call_id=env.call_id))


def released(env: SimpleNamespace) -> int:
    return len(env.trace.of_type("guard_released", call_id=env.call_id))


# ---------------------------------------------------------------------------
# AC-67 ivr
# ---------------------------------------------------------------------------


async def test_ac67_guard_starts_in_ivr_and_drops_all_sonic_audio(g: SimpleNamespace) -> None:
    async with g.chain:
        assert g.guard.state == "ivr"
        await g.sonic.emit_response(SonicResponse(text="Pressing one now.", audio_ms=400))
        await g.sonic.emit_response(SonicResponse(text=STALL_PHRASES[0], audio_ms=200))
        await g.sonic.emit_response(SonicResponse(text="Two.", audio_ms=200, text_first=False))
        await g.sonic.emit_response(SonicResponse(text="", audio_ms=200, with_text=False))
    assert line(g) == []


async def test_ac67_ivr_still_passes_program_dtmf_and_system_frames(g: SimpleNamespace) -> None:
    async with g.chain:
        await g.chain.push(OutputDTMFUrgentFrame.from_string("1"))
        await g.chain.push(InterruptionFrame())
        await g.chain.push(EndFrame())
    tail = g.chain.tail
    assert [f.to_string() for f in tail.dtmf()] == ["1"]
    assert tail.down(StartFrame) and tail.down(InterruptionFrame) and tail.down(EndFrame)


# ---------------------------------------------------------------------------
# AC-68 stall 说法立即放行
# ---------------------------------------------------------------------------


async def test_ac68_stall_phrase_is_released_without_waiting(g: SimpleNamespace) -> None:
    async with g.chain:
        g.guard.set_state("conversation")
        await g.sonic.emit_response(SonicResponse(text=STALL_PHRASES[0], audio_ms=200, chunk_ms=40))
    assert line(g) == response_audio(g.sonic, 1)
    assert len(line(g)) == 5
    assert released(g) >= 1 and blocked(g) == 0
    assert g.guard.counters.released >= 1


async def test_ac68_audio_before_its_text_is_held_until_the_text_arrives(
    compiled: Any, engine: Any, trace: Any, fake_clock: Any, make_fake_sonic: Any, spec: Any
) -> None:
    from waitless.call.output_guard import OutputGuard

    guard = OutputGuard(stall_phrases=compiled.stall_phrases, held_response_max_ms=engine.guard.held_response_max_ms,
                        trace=trace, call_id=spec.call_id, task_id=spec.task_id, clock=fake_clock)
    chain = Chain(guard)
    on_line_when_text_arrived: list[int] = []

    async def sink(frame: Any) -> None:
        if isinstance(frame, LLMTextFrame):
            on_line_when_text_arrived.append(len(chain.tail.tts_audio()))
        await chain.push(frame)

    sonic = make_fake_sonic(sink=sink)
    async with chain:
        guard.set_state("conversation")
        await sonic.emit_response(SonicResponse(text=STALL_PHRASES[1], audio_ms=200, chunk_ms=40, text_first=False))
    assert on_line_when_text_arrived == [0], "audio left the guard before its own text arrived"
    assert audio_bytes(chain.tail.tts_audio()) == response_audio(sonic, 1)


async def test_ac68_text_is_paired_with_its_own_response(g: SimpleNamespace) -> None:
    """第 1 段（擅自作答）扣住；第 2 段是 stall 说法，只放出第 2 段自己的音频。"""
    async with g.chain:
        g.guard.set_state("conversation")
        await g.sonic.emit_response(SonicResponse(text=DECISION_TEXT, audio_ms=400, chunk_ms=40))
        await g.sonic.emit_response(SonicResponse(text=STALL_PHRASES[0], audio_ms=120, chunk_ms=40))
    assert line(g) == response_audio(g.sonic, 2)
    assert len(line(g)) == 3


async def test_ac68_audio_without_text_stays_held(g: SimpleNamespace, fake_clock: Any, engine: Any) -> None:
    async with g.chain:
        g.guard.set_state("conversation")
        await g.sonic.emit_response(SonicResponse(text="", audio_ms=200, chunk_ms=40, with_text=False))
        await fake_clock.advance(engine.guard.held_response_max_ms // 2)
        await g.sonic.emit_response(SonicResponse(text=STALL_PHRASES[0], audio_ms=120, chunk_ms=40))
    assert line(g) == response_audio(g.sonic, 2)


# ---------------------------------------------------------------------------
# AC-69 其余回应等决定
# ---------------------------------------------------------------------------


async def test_ac69_held_response_is_released_by_a_release_decision(g: SimpleNamespace, fake_clock: Any) -> None:
    async with g.chain:
        g.guard.set_state("conversation")
        await fake_clock.advance(2000)
        await register_turn(g.guard, make_turn(call_id=g.call_id, seq=3, t_start_ms=500, t_end_ms=2000, text="What is the service address?"))
        await fake_clock.advance(300)
        await g.sonic.emit_response(SonicResponse(text="The service address is 1200 W Example St.", audio_ms=200, chunk_ms=40))
        assert line(g) == []
        assert await g.guard.release(3) is True
    assert line(g) == response_audio(g.sonic, 1)
    assert released(g) >= 1 and blocked(g) == 0
    assert g.guard.counters.released >= 1


async def test_ac69_response_that_starts_before_the_turn_is_produced_belongs_to_it(g: SimpleNamespace, fake_clock: Any) -> None:
    """A-15：对方 1000 ms 时说完；Sonic 1300 ms 开始回应；TurnDetector 1700 ms 才产出这一轮。回应仍归这一轮。"""
    async with g.chain:
        g.guard.set_state("conversation")
        await fake_clock.advance(1300)
        await g.sonic.emit_response(SonicResponse(text="The service address is 1200 W Example St.", audio_ms=200, chunk_ms=40))
        await fake_clock.advance(400)
        await register_turn(g.guard, make_turn(call_id=g.call_id, seq=1, t_start_ms=0, t_end_ms=1000, text="What is the service address?"))
        assert line(g) == []
        assert await g.guard.release(1) is True
    assert line(g) == response_audio(g.sonic, 1)


async def test_ac69_other_decision_discards_held_response_and_interrupts(g: SimpleNamespace, fake_clock: Any) -> None:
    async with g.chain:
        g.guard.set_state("conversation")
        await fake_clock.advance(1000)
        await register_turn(g.guard, make_turn(call_id=g.call_id, seq=2, t_start_ms=0, t_end_ms=1000, text="Would you like autopay?"))
        await fake_clock.advance(300)
        await g.sonic.emit_response(SonicResponse(text=DECISION_TEXT, audio_ms=200, chunk_ms=40))
        g.guard.set_state("injected")  # 决定是 say（answer_precall）：切到 injected，丢弃这一轮扣住的回应
        await fake_clock.advance(20)
        assert await g.guard.release(2) is False
    assert line(g) == []
    assert g.chain.tail.down(InterruptionFrame), "no interruption went downstream to clear queued audio"
    assert g.chain.head.up(InterruptionFrame), "interruption was not broadcast upstream"
    assert blocked(g) >= 1
    assert g.guard.counters.blocked >= 1


async def test_ac69_discard_drops_the_rounds_audio(g: SimpleNamespace, fake_clock: Any) -> None:
    async with g.chain:
        g.guard.set_state("conversation")
        await fake_clock.advance(1000)
        await register_turn(g.guard, make_turn(call_id=g.call_id, seq=5, t_start_ms=0, t_end_ms=1000, text="Can I sign you up for our survey?"))
        await fake_clock.advance(300)
        await g.sonic.emit_response(SonicResponse(text="Sure, sign me up.", audio_ms=200, chunk_ms=40))
        dropped = await g.guard.discard(5, "decline_offer")
        assert await g.guard.release(5) is False
    assert dropped == len(response_audio(g.sonic, 1)) == 5
    assert line(g) == []
    assert g.chain.tail.down(InterruptionFrame)
    assert blocked(g) >= 1


async def test_ac69_response_held_past_the_limit_is_dropped(g: SimpleNamespace, fake_clock: Any, engine: Any) -> None:
    limit = engine.guard.held_response_max_ms
    async with g.chain:
        g.guard.set_state("conversation")
        await fake_clock.advance(1000)
        await register_turn(g.guard, make_turn(call_id=g.call_id, seq=7, t_start_ms=0, t_end_ms=1000, text="Anything else?"))
        await fake_clock.advance(300)
        await g.sonic.emit_response(SonicResponse(text=DECISION_TEXT, audio_ms=200, chunk_ms=40))
        await fake_clock.advance(limit - 500)
        assert blocked(g) == 0, "dropped before held_response_max_ms"
        await fake_clock.advance(1000)
        await g.sonic.emit_response(SonicResponse(text=STALL_PHRASES[0], audio_ms=120, chunk_ms=40))
        assert blocked(g) >= 1
        assert await g.guard.release(7) is False
    assert line(g) == response_audio(g.sonic, 2)


# ---------------------------------------------------------------------------
# AC-71 注入后的下一段回应放行
# ---------------------------------------------------------------------------


async def test_ac71_injected_state_releases_the_next_response(g: SimpleNamespace) -> None:
    async with g.chain:
        g.guard.set_state("injected")
        await g.sonic.emit_response(SonicResponse(text="We'd like to keep the deposit option.", audio_ms=200, chunk_ms=40))
    assert line(g) == response_audio(g.sonic, 1)


async def test_ac75_guard_writes_no_trace_while_in_handoff(g: SimpleNamespace) -> None:
    """handoff 状态：Sonic 的三段输出全部丢弃，guard 不写任何 trace 事件（不写 guard_blocked，也不写 guard_released）。"""
    async with g.chain:
        g.guard.set_state("handoff")
        before = len(g.trace.events())
        await g.sonic.emit_response(SonicResponse(text=STALL_PHRASES[0], audio_ms=120, chunk_ms=40))
        await g.sonic.emit_response(SonicResponse(text=DECISION_TEXT, audio_ms=200, chunk_ms=40))
        await g.sonic.emit_response(SonicResponse(text="", audio_ms=80, chunk_ms=40, with_text=False))
        after = len(g.trace.events())
    assert line(g) == []
    assert after == before
