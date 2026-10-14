"""E74: one silent response per injection may precede the authorized audio."""

import asyncio

import pytest
from pipecat.frames.frames import (
    InterruptionFrame, LLMFullResponseStartFrame, LLMFullResponseEndFrame,
    OutputAudioRawFrame, TTSAudioRawFrame,
)

from call_engine.conftest import compiled, deps, engine, gate, line_number, settings, spec, store, trace
from call_engine._support import (
    MiniCall, STEP_MS, audio_bytes, handoff_round, make_result, make_turn,
    record_decision, response_audio, settle,
)
from fakes import SonicResponse
from lane_D.test_guard_round_regressions import env, line


IDENTITY_INSTRUCTION = (
    "I am an AI assistant calling on behalf of the account holder, not the account holder."
)
IDENTITY_ANSWER = (
    "One moment, please.\nI am an AI assistant calling on behalf of Alex Example, the account holder."
)


def interrupted_draft(text="One moment, please.\nI am an AI assistant calling on behalf of Alex Example.",
                      *, audio_ms=0):
    return SonicResponse(text=text, final_text='{ "interrupted" : true }',
                         audio_ms=audio_ms, chunk_ms=40, stop_reason="INTERRUPTED")


async def identity_timing(env):
    await env.clock.advance(1000)
    env.guard.on_turn(make_turn(call_id=env.spec.call_id, seq=5, t_start_ms=0,
                               t_end_ms=1000, text="Am I speaking with the account holder?"))
    await env.clock.advance(1000)
    env.guard.set_state("injected")
    await env.clock.advance(108)


async def test_r9_silent_draft_keeps_identity_audio_authorized(env):
    async with env.chain:
        assert env.guard.state == "ivr"
        await identity_timing(env)
        await env.sonic.emit_response(interrupted_draft())
        await env.chain.push(InterruptionFrame())
        await env.clock.advance(1640)
        answer = await env.sonic.emit_response(SonicResponse(text=IDENTITY_ANSWER, audio_ms=600, chunk_ms=40))
        await env.clock.advance(env.engine.guard.held_response_max_ms)
        assert line(env) == response_audio(env.sonic, answer)
        assert [e.action["reason"] for e in env.trace.of_type("guard_released")] == ["injected", "injected"]
        assert not env.trace.of_type("guard_blocked")
        assert env.guard.state == "conversation"


async def test_r9_new_injection_renews_silent_exemption(env):
    async with env.chain:
        await identity_timing(env)
        await env.sonic.emit_response(interrupted_draft())
        first = await env.sonic.emit_response(SonicResponse(text="First authorized answer.", audio_ms=120))
        assert env.guard.state == "conversation"
        env.guard.set_state("injected")
        await env.sonic.emit_response(interrupted_draft("Another old draft."))
        second = await env.sonic.emit_response(SonicResponse(text="Second authorized answer.", audio_ms=120))
        assert line(env) == response_audio(env.sonic, first) + response_audio(env.sonic, second)
        assert env.guard.state == "conversation"


async def test_r9_second_silent_response_consumes_injection(env):
    async with env.chain:
        await identity_timing(env)
        await env.sonic.emit_response(interrupted_draft())
        await env.sonic.emit_response(interrupted_draft("Second silent response."))
        await env.sonic.emit_response(SonicResponse(text="Unreviewed third response.", audio_ms=120))
        assert line(env) == []
        assert env.guard.state == "conversation"
        await env.clock.advance(env.engine.guard.held_response_max_ms)
        assert env.trace.of_type("guard_blocked")[-1].action["reason"] == "timeout"


async def test_r9_interrupted_audible_response_consumes_injection(env):
    async with env.chain:
        await identity_timing(env)
        audible = await env.sonic.emit_response(interrupted_draft(audio_ms=40))
        await env.chain.push(InterruptionFrame())
        await env.sonic.emit_response(SonicResponse(text="Unreviewed response to the interruption.", audio_ms=120))
        assert line(env) == response_audio(env.sonic, audible)
        assert env.guard.state == "conversation"


async def test_r9_identity_draft_then_answer_records_and_checks_once(spec, deps, engine, trace,
                                                                  gate, make_fake_sonic):
    async with MiniCall(spec=spec, deps=deps, engine=engine, make_sonic=make_fake_sonic) as mc:
        mc.tap.guard = mc.guard
        result = record_decision(trace, make_result("say", intent="state_ai_identity",
            say_text=IDENTITY_INSTRUCTION), task_id=spec.task_id, call_id=spec.call_id, turn_seq=5)
        await mc.actions.execute(result)
        await mc.sonic.emit_response(interrupted_draft())
        answer = await mc.sonic.emit_response(SonicResponse(text=IDENTITY_ANSWER, audio_ms=600))
        await settle()
        assert audio_bytes(mc.line_audio()) == response_audio(mc.sonic, answer)
        assert [e.transcript for e in trace.of_type("waitless_utterance")] == [IDENTITY_ANSWER]
        requests = gate.said_requests()
        assert len(requests) == 1
        assert requests[0].attempt == 0
        assert requests[0].transcript[-1].text == IDENTITY_ANSWER
        assert not trace.of_type("guard_blocked")
        await asyncio.gather(*mc.actions._tasks)


async def test_r9_handoff_draft_then_announce_finishes(spec, deps, engine, trace,
                                                      fake_clock, make_fake_sonic):
    async with MiniCall(spec=spec, deps=deps, engine=engine, make_sonic=make_fake_sonic) as mc:
        mc.tap.guard = mc.guard
        mc.guard.set_state("conversation")
        result = record_decision(trace, handoff_round(), task_id=spec.task_id,
                                 call_id=spec.call_id, turn_seq=5)
        task = asyncio.create_task(mc.actions.execute(result))
        try:
            await settle()
            assert mc.sent_injections() == [spec.compiled.say_templates["handoff.announce"]]
            await mc.sonic.emit_response(interrupted_draft())
            announce = await mc.sonic.emit_response(SonicResponse(
                text=spec.compiled.say_templates["handoff.announce"], audio_ms=600))
            for _ in range(engine.guard.held_response_max_ms // STEP_MS + 1):
                await fake_clock.advance(STEP_MS)
                await settle()
                if task.done():
                    break
            assert task.done(), "handoff must finish after the authorized announce audio"
            assert task.result().ok
            assert [e.event_type for e in trace.events(call_id=spec.call_id)
                    if e.event_type.startswith("handoff_")] == [
                        "handoff_announced", "handoff_ready", "handoff_started"]
            assert audio_bytes(mc.line_audio()) == response_audio(mc.sonic, announce)
        finally:
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)


async def test_r9_ending_keeps_goodbye_authorized_after_silent_response(env):
    async with env.chain:
        env.guard.set_state("ending")
        await env.sonic.emit_response(interrupted_draft())
        goodbye = await env.sonic.emit_response(SonicResponse(text="Thank you. Goodbye.", audio_ms=120))
        assert line(env) == response_audio(env.sonic, goodbye)
        assert env.guard.state == "ending"


async def test_r9_executor_tail_does_not_count_as_response_audio(env):
    async with env.chain:
        env.guard.set_state("injected")
        await env.chain.push(LLMFullResponseStartFrame())
        tail = OutputAudioRawFrame(audio=bytes(80), sample_rate=env.engine.audio.pipeline_in_sample_rate,
                                   num_channels=1)
        tail._waitless_dtmf_tail = True
        await env.chain.push(tail)
        await env.chain.push(LLMFullResponseEndFrame())
        assert env.guard.state == "injected"
        answer = await env.sonic.emit_response(SonicResponse(text="Authorized answer.", audio_ms=120))
        assert env.chain.tail.down(OutputAudioRawFrame)[0] is tail
        assert line(env) == response_audio(env.sonic, answer)
        assert env.guard.state == "conversation"


@pytest.mark.parametrize("frame_type", [OutputAudioRawFrame, TTSAudioRawFrame])
async def test_r9_each_output_audio_type_consumes_injection(env, frame_type):
    async with env.chain:
        env.guard.set_state("injected")
        await env.chain.push(LLMFullResponseStartFrame())
        audio = frame_type(audio=bytes(80), sample_rate=env.engine.audio.pipeline_in_sample_rate,
                           num_channels=1)
        await env.chain.push(audio)
        await env.chain.push(InterruptionFrame())
        await env.chain.push(LLMFullResponseEndFrame())
        await env.sonic.emit_response(SonicResponse(text="Unreviewed next answer.", audio_ms=120))
        assert env.chain.tail.down(OutputAudioRawFrame) == [audio]
        assert env.guard.state == "conversation"


async def test_r9_old_response_end_cannot_spend_new_generation_exemption(env):
    async with env.chain:
        env.guard.set_state("injected")
        await env.chain.push(LLMFullResponseStartFrame())
        env.guard.set_state("injected")
        await env.chain.push(LLMFullResponseEndFrame())
        await env.sonic.emit_response(interrupted_draft())
        assert env.guard.state == "injected"
        answer = await env.sonic.emit_response(SonicResponse(text="New injection answer.", audio_ms=120))
        assert line(env) == response_audio(env.sonic, answer)
        assert env.guard.state == "conversation"
