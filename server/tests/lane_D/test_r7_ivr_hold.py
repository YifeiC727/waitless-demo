"""I2A-1: keep IVR drafts until a decision releases or discards them."""

import asyncio

import pytest
from pipecat.frames.frames import InterruptionFrame, LLMTextFrame

from call_engine.conftest import compiled, engine, trace, spec, line_number, settings, gate, store
from call_engine._support import (
    LiveCallHarness, make_result, make_turn, register_turn, response_audio,
    audio_bytes, patch_menu_parser, settle, wait_until,
)
from lane_D.test_guard_round_regressions import env, line, emit
from fakes import SonicResponse


async def register_speech(env):
    await env.clock.advance(1700)
    await register_turn(env.guard, make_turn(call_id=env.spec.call_id, seq=2,
        t_start_ms=0, t_end_ms=1000, text="How can I help?"))
    await env.clock.advance(374)


@pytest.mark.parametrize("text_first", [True, False])
async def test_ivr_draft_survives_transition_and_releases_all_audio(env, text_first):
    async with env.chain:
        await register_speech(env)
        await env.sonic.emit_response(SonicResponse(text="Please start service.",
            audio_ms=200, text_first=text_first))
        assert line(env) == []
        await env.clock.advance(165)
        env.guard.set_state("conversation")
        assert await env.guard.release(2)
        assert line(env) == response_audio(env.sonic, 1)
        assert not env.trace.of_type("guard_blocked")


async def test_ivr_stall_needs_decision_even_after_transition(env):
    async with env.chain:
        await register_speech(env)
        await emit(env, env.compiled.stall_phrases[0])
        assert line(env) == []
        env.guard.set_state("conversation")
        assert line(env) == []
        assert await env.guard.release(2)
        assert line(env) == response_audio(env.sonic, 1)


async def test_ivr_release_does_not_leave_approval(env):
    async with env.chain:
        await register_speech(env)
        assert not await env.guard.release(2)
        await emit(env)
        assert not await env.guard.release(2)
        env.guard.set_state("conversation")
        await env.chain.push(LLMTextFrame("A later text fragment."))
        assert line(env) == []
        assert await env.guard.release(2)
        assert line(env) == response_audio(env.sonic, 1)


@pytest.mark.parametrize("state", ["ivr", "injected", "handoff", "ending"])
async def test_ivr_discard_and_state_change_clear_unowned_draft(env, state):
    async with env.chain:
        await env.clock.advance(1700)
        await register_turn(env.guard, make_turn(call_id=env.spec.call_id, seq=2,
            t_start_ms=0, t_end_ms=1000, text="For billing press 1.", menu=[("1", "billing")]))
        await emit(env)
        await emit(env, "Unowned extra draft.")
        assert await env.guard.discard(2, "press") == len(response_audio(env.sonic, 1))
        env.guard.set_state(state)
        await settle()
        env.guard.set_state("conversation")
        assert not await env.guard.release(2)
        assert line(env) == []
        assert [e.action["reason"] for e in env.trace.of_type("guard_blocked")] == ["ivr", "ivr"]
        assert env.chain.tail.down(InterruptionFrame)
        assert env.chain.head.up(InterruptionFrame)


async def test_ivr_timeout_discards_and_interrupts(env):
    async with env.chain:
        await register_speech(env)
        await emit(env)
        assert not env.trace.of_type("guard_blocked")
        await env.clock.advance(env.engine.guard.held_response_max_ms)
        env.guard.set_state("conversation")
        assert not await env.guard.release(2)
        assert line(env) == []
        assert [e.action["reason"] for e in env.trace.of_type("guard_blocked")] == ["ivr"]
        assert env.chain.tail.down(InterruptionFrame)
        assert env.chain.head.up(InterruptionFrame)


async def test_live_menu_then_speech_releases_draft_before_decision(
    monkeypatch, settings, spec, fake_clock, trace, gate, make_fake_sonic, fake_carrier, store, engine,
):
    patch_menu_parser(monkeypatch)
    gate.round_script.append(make_result("press", digits="1", call_phase="ivr_menu"))
    gate.round_script.append(make_result("release", intent="open_answer", call_phase="ai_agent"))
    original = gate.decide_round
    pending, proceed = asyncio.Event(), asyncio.Event()

    async def delayed(turn, ctx):
        if turn.kind == "speech":
            pending.set()
            await proceed.wait()
        return await original(turn, ctx)

    monkeypatch.setattr(gate, "decide_round", delayed)
    sonic = make_fake_sonic()
    h = LiveCallHarness(monkeypatch=monkeypatch, settings=settings, spec=spec,
        clock=fake_clock, trace=trace, gate=gate, sonic=sonic, carrier=fake_carrier, store=store)
    try:
        await h.start()
        await h.connect_line()
        await h.company_says("For billing press 1.", silence_ms=engine.turns.menu_end_silence_ms + engine.turns.transcript_grace_ms)
        assert h.call._guard.state == "ivr"
        await h.company_says("How can I help?", silence_ms=engine.turns.turn_end_silence_ms + engine.turns.transcript_grace_ms)
        await wait_until(pending.is_set, what="speech decision waiting")
        await h.sonic_says(SonicResponse(text="Please start service.", audio_ms=200))
        assert not h.transport.output().tts_audio()
        proceed.set()
        await settle(200)
        assert audio_bytes(h.transport.output().tts_audio()) == response_audio(sonic, 1)
    finally:
        proceed.set()
        await h.close()
