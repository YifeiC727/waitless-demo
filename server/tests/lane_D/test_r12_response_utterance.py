"""E76/E77: released FINAL blocks form one response utterance."""
import asyncio
import pytest
from pipecat.frames.frames import LLMFullResponseEndFrame, LLMFullResponseStartFrame
from call_engine.conftest import compiled, deps, engine, gate, line_number, settings, spec, store, trace
from call_engine._support import MiniCall, make_result, record_decision, settle, wait_until
from fakes import SonicResponse
from waitless.models import ProviderTextEvent

IDENTITY = "I am an AI assistant calling on behalf of the account holder, not the account holder."
CHUNKS = ["One moment, please. Let me check that with the account holder.",
          "\n\nI am an AI assistant calling on behalf of Alex Example.", " I am not the account holder."]

async def text(mc, stage, words, number, stop="PARTIAL_TURN"):
    await mc.tap.on_provider_text(ProviderTextEvent(role="ASSISTANT", stage=stage, text=words,
        response=number, stop_reason=stop, t_ms=mc.clock.monotonic_ms()))
    await settle()

async def multi_chunk(mc, chunks):
    original = mc.sonic._text_event
    async def hold_final(role, stage, words, stop, response):
        if stage != "FINAL":
            await original(role, stage, words, stop, response)
    mc.sonic._text_event = hold_final
    try:
        n = await mc.sonic.emit_response(SonicResponse(text=chunks[0], audio_ms=200, stop_reason="PARTIAL_TURN"))
    finally:
        mc.sonic._text_event = original
    for extra in chunks[1:]:
        await text(mc, "SPECULATIVE", extra, n)
    return n

def utterances(trace):
    return trace.of_type("waitless_utterance")

def decision(trace, spec, action="say"):
    result = make_result(action, say_text=IDENTITY if action == "say" else None, call_phase="live_rep")
    if action == "say":
        result = result.model_copy(update={"intent": "state_ai_identity"})
    return record_decision(trace, result, task_id=spec.task_id, call_id=spec.call_id, turn_seq=5)

async def test_r12_identity_checks_exact_whole_response(spec, deps, engine, trace, gate, make_fake_sonic):
    gate.said.append("pass")
    async with MiniCall(spec=spec, deps=deps, engine=engine, make_sonic=make_fake_sonic) as mc:
        mc.tap.guard = mc.guard
        await mc.actions.execute(decision(trace, spec))
        n = await multi_chunk(mc, CHUNKS)
        await text(mc, "FINAL", CHUNKS[0], n)
        assert gate.said_requests() == []
        assert utterances(trace) == []
        await text(mc, "FINAL", CHUNKS[1], n)
        await mc.clock.advance(20)
        await text(mc, "FINAL", CHUNKS[2], n, "END_TURN")
        assert [e.transcript for e in utterances(trace)] == ["".join(CHUNKS)]
        assert utterances(trace)[0].t_ms == mc.clock.monotonic_ms()
        assert [r.transcript[-1].text for r in gate.said_requests()] == ["".join(CHUNKS)]
        assert mc.sent_injections() == [IDENTITY]

@pytest.mark.parametrize("action", ["handoff", "end"])
async def test_r12_announcement_waits_for_last_final(action, spec, deps, engine, trace, make_fake_sonic):
    async with MiniCall(spec=spec, deps=deps, engine=engine, make_sonic=make_fake_sonic) as mc:
        mc.tap.guard = mc.guard
        task = asyncio.create_task(mc.actions.execute(decision(trace, spec, action)))
        chunks = [CHUNKS[0], " I will bring the account holder into this call." if action == "handoff"
                  else " The account holder will follow up if needed. Goodbye."]
        try:
            await wait_until(lambda: mc.sonic.injections)
            n = await multi_chunk(mc, chunks)
            await text(mc, "FINAL", chunks[0], n)
            await mc.clock.advance(engine.handoff.announce_idle_ms + 100)
            await settle()
            assert not trace.of_type("handoff_ready")
            assert "hangup" not in mc.carrier.names()
            assert not task.done()
            await text(mc, "FINAL", chunks[1], n, "END_TURN")
            await mc.clock.advance(engine.handoff.announce_idle_ms + 100)
            await settle()
            assert task.done() and task.result().ok
            assert [e.transcript for e in utterances(trace)] == ["".join(chunks)]
            if action == "handoff":
                assert [e.event_type for e in trace.events() if e.event_type.startswith("handoff_")] == [
                    "handoff_announced", "handoff_ready", "handoff_started"]
            else:
                assert mc.carrier.names() == ["hangup"]
                assert deps.on_ended.calls == [spec.call_id]
        finally:
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)

@pytest.mark.parametrize("stop_control", [True, False])
async def test_r12_interruption_completes_received_blocks(stop_control, spec, deps, engine, trace, gate, make_fake_sonic):
    gate.said.append("pass")
    async with MiniCall(spec=spec, deps=deps, engine=engine, make_sonic=make_fake_sonic) as mc:
        mc.tap.guard = mc.guard
        await mc.actions.execute(decision(trace, spec))
        n = await multi_chunk(mc, CHUNKS[:2])
        if stop_control:
            await text(mc, "FINAL", CHUNKS[0], n)
            assert utterances(trace) == []
            await text(mc, "FINAL", '{ "interrupted" : true }', n)
        else:
            await text(mc, "FINAL", CHUNKS[0], n, "INTERRUPTED")
        assert [e.transcript for e in utterances(trace)] == [CHUNKS[0]]
        assert len(gate.said_requests()) == 1
        await text(mc, "FINAL", CHUNKS[1], n)
        assert len(utterances(trace)) == len(gate.said_requests()) == 1

async def test_r12_timeout_starts_at_response_end(spec, deps, engine, trace, make_fake_sonic):
    async with MiniCall(spec=spec, deps=deps, engine=engine, make_sonic=make_fake_sonic) as mc:
        mc.tap.guard = mc.guard
        mc.guard.set_state("injected")
        n = await multi_chunk(mc, CHUNKS[:2])
        await text(mc, "FINAL", CHUNKS[0], n)
        assert utterances(trace) == []
        await mc.clock.advance(engine.guard.held_response_max_ms - 1)
        assert utterances(trace) == []
        await mc.clock.advance(1)
        assert [e.transcript for e in utterances(trace)] == [CHUNKS[0]]
        await text(mc, "FINAL", CHUNKS[1], n)
        assert len(utterances(trace)) == 1

async def test_r12_newer_final_completes_older_with_its_own_timestamp(spec, deps, engine, trace, make_fake_sonic):
    async with MiniCall(spec=spec, deps=deps, engine=engine, make_sonic=make_fake_sonic) as mc:
        mc.tap.guard = mc.guard
        mc.guard.set_state("ending")
        n = await multi_chunk(mc, CHUNKS[:2])
        await text(mc, "FINAL", CHUNKS[0], n)
        before = mc.clock.monotonic_ms()
        assert utterances(trace) == []
        await mc.clock.advance(20)
        await mc.sonic.emit_response(SonicResponse(text="New response.", audio_ms=200))
        assert [e.transcript for e in utterances(trace)] == [CHUNKS[0], "New response."]
        assert utterances(trace)[0].t_ms == before

async def test_r12_final_before_end_completes_at_end(spec, deps, engine, trace, make_fake_sonic):
    async with MiniCall(spec=spec, deps=deps, engine=engine, make_sonic=make_fake_sonic) as mc:
        mc.tap.guard = mc.guard
        mc.guard.set_state("injected")
        await mc.chain.push(LLMFullResponseStartFrame())
        await text(mc, "SPECULATIVE", CHUNKS[0], 1)
        await text(mc, "FINAL", CHUNKS[0], 1)
        assert utterances(trace) == []
        await mc.chain.push(LLMFullResponseEndFrame())
        assert [e.transcript for e in utterances(trace)] == [CHUNKS[0]]

async def test_r12_pending_response_dropped_across_handoff(spec, deps, engine, trace, make_fake_sonic):
    async with MiniCall(spec=spec, deps=deps, engine=engine, make_sonic=make_fake_sonic) as mc:
        mc.tap.guard = mc.guard
        mc.guard.set_state("injected")
        n = await multi_chunk(mc, CHUNKS[:2])
        await text(mc, "FINAL", CHUNKS[0], n)
        assert utterances(trace) == []
        mc.media_gate.in_handoff = True
        mc.guard.set_state("handoff")
        await mc.clock.advance(engine.guard.held_response_max_ms)
        mc.media_gate.in_handoff = False
        mc.guard.set_state("conversation")
        await text(mc, "FINAL", CHUNKS[1], n)
        assert utterances(trace) == []

async def test_r12_single_block_guardrail(spec, deps, engine, trace, gate, make_fake_sonic):
    gate.said.append("pass")
    async with MiniCall(spec=spec, deps=deps, engine=engine, make_sonic=make_fake_sonic) as mc:
        mc.tap.guard = mc.guard
        await mc.actions.execute(decision(trace, spec))
        await mc.sonic.emit_response(SonicResponse(text=IDENTITY, audio_ms=200))
        await settle()
        assert [e.transcript for e in utterances(trace)] == [IDENTITY]
        assert len(gate.said_requests()) == 1
