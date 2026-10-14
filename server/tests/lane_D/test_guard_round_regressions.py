"""D-r3: a decision owns only the first non-stall response in its interval."""

from types import SimpleNamespace

import pytest
from pipecat.frames.frames import LLMFullResponseStartFrame, LLMFullResponseEndFrame, LLMTextFrame, TTSAudioRawFrame

from call_engine.conftest import compiled, engine, trace, spec, line_number
from call_engine._support import Chain, audio_bytes, make_turn, response_audio
from fakes import SonicResponse
from waitless.call.output_guard import OutputGuard


@pytest.fixture
def env(compiled, engine, trace, spec, fake_clock, make_fake_sonic):
    guard = OutputGuard(stall_phrases=compiled.stall_phrases,
                        held_response_max_ms=engine.guard.held_response_max_ms,
                        trace=trace, call_id=spec.call_id, task_id=spec.task_id, clock=fake_clock)
    chain = Chain(guard)
    return SimpleNamespace(guard=guard, chain=chain, sonic=make_fake_sonic(sink=chain.push),
                           clock=fake_clock, trace=trace, spec=spec, compiled=compiled, engine=engine)


def turn(env, seq=1, start=0, end=100):
    return make_turn(call_id=env.spec.call_id, seq=seq, t_start_ms=start, t_end_ms=end, text="Please answer.")


async def emit(env, text="First answer."):
    await env.sonic.emit_response(SonicResponse(text=text, audio_ms=120, chunk_ms=40))


def line(env):
    return audio_bytes(env.chain.tail.tts_audio())


@pytest.mark.parametrize("register_late", [False, True])
@pytest.mark.parametrize("same_timestamp", [False, True])
async def test_release_only_first_response(env, register_late, same_timestamp):
    async with env.chain:
        env.guard.set_state("conversation")
        if not register_late:
            env.guard.on_turn(turn(env))
        await env.clock.advance(200)
        await emit(env)
        if not same_timestamp:
            await env.clock.advance(20)
        await emit(env, "Later unauthorized answer.")
        if register_late:
            env.guard.on_turn(turn(env))
        assert await env.guard.release(1)
        assert line(env) == response_audio(env.sonic, 1)
        assert not await env.guard.release(1)
        await env.clock.advance(env.engine.guard.held_response_max_ms + 1)
        assert line(env) == response_audio(env.sonic, 1)
        assert any(e.action.get("reason") == "timeout" for e in env.trace.of_type("guard_blocked"))


async def test_discard_only_first_response(env):
    async with env.chain:
        env.guard.set_state("conversation")
        env.guard.on_turn(turn(env))
        await env.clock.advance(200)
        await emit(env)
        await env.clock.advance(20)
        await emit(env, "Later unauthorized answer.")
        assert await env.guard.discard(1, "decline_offer") == len(response_audio(env.sonic, 1))
        assert not await env.guard.release(1)
        assert line(env) == []
        assert len(env.trace.of_type("guard_blocked")) == 1
        await env.clock.advance(env.engine.guard.held_response_max_ms + 1)
        assert len(env.trace.of_type("guard_blocked")) == 2


async def test_late_text_after_approval_only_releases_first_response(env):
    async with env.chain:
        env.guard.set_state("conversation")
        env.guard.on_turn(turn(env))
        await env.clock.advance(200)
        await env.chain.push(LLMFullResponseStartFrame())
        audio = TTSAudioRawFrame(audio=bytes(80), sample_rate=env.engine.audio.pipeline_in_sample_rate, num_channels=1)
        await env.chain.push(audio)
        assert await env.guard.release(1)
        assert line(env) == []
        await env.chain.push(LLMTextFrame("First answer."))
        await env.chain.push(LLMFullResponseEndFrame())
        assert line(env) == [audio.audio]
        await env.clock.advance(20)
        await emit(env, "Later unauthorized answer.")
        assert line(env) == [audio.audio]
        await env.clock.advance(20)
        await emit(env, env.compiled.stall_phrases[0])
        assert line(env) == [audio.audio] + response_audio(env.sonic, 2)


@pytest.mark.parametrize("approve_first", [False, True])
async def test_stall_does_not_consume_first_response(env, approve_first):
    async with env.chain:
        env.guard.set_state("conversation")
        env.guard.on_turn(turn(env))
        await env.clock.advance(200)
        if approve_first:
            assert not await env.guard.release(1)
        await emit(env, env.compiled.stall_phrases[0])
        await env.clock.advance(20)
        await emit(env)
        await env.clock.advance(20)
        await emit(env, "Later unauthorized answer.")
        if not approve_first:
            assert await env.guard.release(1)
        assert line(env) == response_audio(env.sonic, 1) + response_audio(env.sonic, 2)


async def test_next_turn_owns_its_own_first_response(env):
    async with env.chain:
        env.guard.set_state("conversation")
        env.guard.on_turn(turn(env))
        env.guard.on_turn(turn(env, seq=2, start=300, end=400))
        await env.clock.advance(200)
        await emit(env)
        await env.clock.advance(20)
        await emit(env, "Extra first-round answer.")
        await env.clock.advance(280)
        await emit(env, "Second-round answer.")
        await env.clock.advance(20)
        await emit(env, "Extra second-round answer.")
        assert await env.guard.release(1)
        assert await env.guard.release(2)
        assert line(env) == response_audio(env.sonic, 1) + response_audio(env.sonic, 3)
