"""D-F1: only a sent announcement's newer FINAL and released audio allow completion."""
import asyncio

import pytest
from pipecat.frames.frames import LLMFullResponseStartFrame, LLMFullResponseEndFrame, TTSAudioRawFrame
from call_engine.conftest import compiled, deps, engine, gate, line_number, settings, spec, store, trace
from call_engine._support import MiniCall, make_result, record_decision, settle, tone_pcm16, wait_until
from fakes import SonicResponse
from waitless.models import ProviderTextEvent


@pytest.mark.parametrize("action", ["handoff", "end"])
@pytest.mark.parametrize("queued", [False, True])
async def test_wait_for_sent_new_response_and_audio(action, queued, spec, deps, engine, trace,
                                                   fake_clock, make_fake_sonic):
    async with MiniCall(spec=spec, deps=deps, engine=engine, make_sonic=make_fake_sonic) as mc:
        mc.guard.set_state("injected")
        await mc.sonic.emit_response(SonicResponse(text="Earlier response", audio_ms=200))
        mc.guard.set_state("conversation")
        if queued:
            mc.sonic.begin_continuation()
        result = record_decision(trace, make_result(action, call_phase="live_rep"),
                                 task_id=spec.task_id, call_id=spec.call_id, turn_seq=0)
        task = asyncio.create_task(mc.actions.execute(result))
        try:
            await settle()
            async def final(number):
                await mc.tap.on_provider_text(ProviderTextEvent(role="ASSISTANT", stage="FINAL",
                    text="Earlier response", response=number, stop_reason="END_TURN", t_ms=fake_clock.monotonic_ms()))
            if queued:
                await final(2)  # FINAL while announce/goodbye is still queued.
                await fake_clock.advance(engine.handoff.announce_idle_ms + 100)
                assert not task.done()
                assert not mc.media_gate.in_handoff and "hangup" not in mc.carrier.names()
                mc.sonic.complete_continuation()
                await mc.injector.flush()
            await wait_until(lambda: mc.sonic.injections, what="actual injection")
            mc.actions._checks.append(("A previous say", 0))
            await final(1)  # Previously started response must still go to DP-C2.
            await settle()
            assert not mc.actions._checks
            await fake_clock.advance(engine.handoff.announce_idle_ms + 100)
            assert not task.done()
            assert not trace.of_type("handoff_ready", call_id=spec.call_id)
            assert "hangup" not in mc.carrier.names()
            await final(2)
            await fake_clock.advance(engine.handoff.announce_idle_ms + 100)
            assert not task.done(), "a newer FINAL without released audio is insufficient"
            await mc.chain.push(LLMFullResponseStartFrame())
            await mc.chain.push(TTSAudioRawFrame(audio=tone_pcm16(20, 24000), sample_rate=24000, num_channels=1))
            await mc.chain.push(LLMFullResponseEndFrame())
            await fake_clock.advance(engine.handoff.announce_idle_ms - 1)
            assert not task.done()
            await fake_clock.advance(1)
            await settle()
            assert task.done() and task.result().ok
            if action == "handoff":
                events = trace.events(call_id=spec.call_id)
                order = [e.event_type for e in events if e.event_type.startswith("handoff_")]
                assert order == ["handoff_announced", "handoff_ready", "handoff_started"]
                assert mc.media_gate.in_handoff
            else:
                assert mc.carrier.names() == ["hangup"]
                assert deps.on_ended.calls == [spec.call_id]
        finally:
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
            await asyncio.gather(*mc.actions._tasks, return_exceptions=True)
