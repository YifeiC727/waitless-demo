"""E75: a check belongs to a response starting after its actual injection send."""

import asyncio

import pytest

from call_engine.conftest import compiled, deps, engine, gate, line_number, settings, spec, store, trace
from call_engine._support import MiniCall, make_result, record_decision, settle
from fakes import SonicResponse
from waitless.models import ProviderTextEvent


INSTRUCTION = "The account holder chose: Pay a security deposit."
FIRST_BLOCK = "One moment, please."
LEFTOVER_BLOCK = " Once entered, please proceed with verification."
ANSWER = "We will pay a security deposit."


def say_decision(trace, spec):
    return record_decision(trace, make_result("say", say_text=INSTRUCTION,
        intent="answer_precall:deposit_or_autopay"), task_id=spec.task_id,
        call_id=spec.call_id, turn_seq=5)


async def final(mc, text, response):
    await mc.tap.on_provider_text(ProviderTextEvent(role="ASSISTANT", stage="FINAL", text=text,
        response=response, stop_reason="END_TURN", t_ms=mc.clock.monotonic_ms()))
    await settle()


async def test_r9_reinject_checks_new_response_and_records_old_leftover(spec, deps, engine,
                                                                     trace, gate, make_fake_sonic):
    gate.said.extend(["fail", "pass"])
    async with MiniCall(spec=spec, deps=deps, engine=engine, make_sonic=make_fake_sonic) as mc:
        mc.tap.guard = mc.guard
        mc.guard.set_state("conversation")
        await mc.actions.execute(say_decision(trace, spec))
        old = await mc.sonic.emit_response(SonicResponse(text=FIRST_BLOCK, audio_ms=120))
        await settle()
        assert mc.sent_injections() == [INSTRUCTION, INSTRUCTION]
        await final(mc, LEFTOVER_BLOCK, old)
        await mc.sonic.emit_response(SonicResponse(text=ANSWER, audio_ms=120))
        await settle()
        assert [r.transcript[-1].text for r in gate.said_requests()] == [FIRST_BLOCK, ANSWER]
        assert [r.attempt for r in gate.said_requests()] == [0, 1]
        # The first response already finalized (one SPECULATIVE, one FINAL).
        # A late extra block cannot create a second utterance or consume the retry.
        assert [e.transcript for e in trace.of_type("waitless_utterance")] == [FIRST_BLOCK, ANSWER]
        assert not mc.actions._checks
        assert not mc.injector._send_observers
        await asyncio.gather(*mc.actions._tasks)


@pytest.mark.parametrize("counter_source", ["provider", "guard"])
async def test_r9_queued_say_binds_counter_at_actual_send(counter_source, spec, deps, engine,
                                                       trace, gate, make_fake_sonic):
    async with MiniCall(spec=spec, deps=deps, engine=engine, make_sonic=make_fake_sonic) as mc:
        # Keep the tap's default guard=None to isolate the send/check boundary.
        mc.sonic.begin_continuation()
        result = say_decision(trace, spec)
        await mc.actions.execute(result)
        assert mc.injector.pending == 1
        await final(mc, "Final while the instruction is queued.", 1)
        assert not gate.said_requests()

        # Provider frames may be ahead of the guard, or vice versa. Neither counter
        # existed when inject() queued the instruction; snapshot their max at flush().
        if counter_source == "provider":
            mc.sonic._waitless_response = 3
        else:
            mc.guard._response_number = 3
        mc.sonic.complete_continuation()
        assert await mc.injector.flush() == 1
        assert result.decision_id not in mc.injector._send_observers
        await final(mc, "Final from a response started before the send.", 3)
        assert not gate.said_requests()
        await final(mc, ANSWER, 4)
        assert [r.transcript[-1].text for r in gate.said_requests()] == [ANSWER]
        assert not mc.actions._checks
        await asyncio.gather(*mc.actions._tasks)


async def test_r9_response_none_retains_next_final_fallback(spec, deps, engine,
                                                         trace, gate, make_fake_sonic):
    async with MiniCall(spec=spec, deps=deps, engine=engine, make_sonic=make_fake_sonic) as mc:
        await mc.actions.execute(say_decision(trace, spec))
        await final(mc, ANSWER, None)
        assert [r.transcript[-1].text for r in gate.said_requests()] == [ANSWER]
        assert not mc.actions._checks
        assert not mc.injector._send_observers
        await asyncio.gather(*mc.actions._tasks)


async def test_r9_numbered_final_waits_for_send_completion(spec, deps, engine, trace, gate,
                                                        make_fake_sonic, monkeypatch):
    async with MiniCall(spec=spec, deps=deps, engine=engine, make_sonic=make_fake_sonic) as mc:
        entered, finish = asyncio.Event(), asyncio.Event()
        original_send = mc.sonic._send_text_event

        async def delayed_send(**kwargs):
            entered.set()
            await finish.wait()
            await original_send(**kwargs)

        monkeypatch.setattr(mc.sonic, "_send_text_event", delayed_send)
        task = asyncio.create_task(mc.actions.execute(say_decision(trace, spec)))
        try:
            await entered.wait()
            await final(mc, "Final while the send is still in progress.", 1)
            assert not gate.said_requests()
            finish.set()
            await task
            await final(mc, ANSWER, 2)
            assert [r.transcript[-1].text for r in gate.said_requests()] == [ANSWER]
            assert not mc.injector._send_observers
            await asyncio.gather(*mc.actions._tasks)
        finally:
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
