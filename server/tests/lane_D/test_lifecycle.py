"""Additional lifecycle checks using the public fakes, without network access."""
import asyncio

from call_engine.conftest import compiled, deps, engine, gate, line_number, settings, spec, store, trace
from call_engine._support import (
    Chain, make_result, record_decision,
)
from fakes import SonicResponse
from waitless.call.injector import SonicTextInjector
from waitless.call.output_guard import OutputGuard
from pipecat.frames.frames import LLMContextFrame


async def test_continuation_observed_before_any_injection(make_fake_sonic, trace, spec, fake_clock):
    sonic = make_fake_sonic()
    injector = SonicTextInjector(sonic, trace=trace, call_id=spec.call_id,
        task_id=spec.task_id, clock=fake_clock)
    sonic.begin_continuation()
    await injector.flush()  # LiveCall's periodic poll must observe an empty queue too.
    sonic.clear_handoff_flag()
    decision = record_decision(trace, make_result("say", say_text="Continue our task."),
        task_id=spec.task_id, call_id=spec.call_id, turn_seq=1)
    await injector.inject(decision.say_text, decision.decision_id)
    assert injector.pending == 1
    assert not sonic.injections
    sonic.swap_stream()
    assert await injector.flush() == 1
    from waitless.models import SONIC_INSTRUCTION_PREFIX
    assert sonic.sent_texts == [SONIC_INSTRUCTION_PREFIX + decision.say_text]


async def test_live_call_seeds_sonic_context(spec, settings, store, gate, trace, fake_clock,
                                             make_fake_sonic, fake_carrier, monkeypatch):
    from call_engine._support import LiveCallHarness, patch_menu_parser, wait_until
    patch_menu_parser(monkeypatch)
    sonic = make_fake_sonic()
    contexts = []
    original = sonic.process_frame
    async def capture(frame, direction):
        if isinstance(frame, LLMContextFrame):
            contexts.append(frame.context)
        await original(frame, direction)
    monkeypatch.setattr(sonic, "process_frame", capture)
    harness = LiveCallHarness(monkeypatch=monkeypatch, settings=settings, spec=spec,
        clock=fake_clock, trace=trace, gate=gate, sonic=sonic, carrier=fake_carrier, store=store)
    try:
        await harness.start()
        await harness.connect_line()
        await wait_until(lambda: contexts or harness.line_task.done(), what="initial Sonic context")
        assert len(contexts) == 1
        assert contexts[0].get_messages() == []
    finally:
        await harness.close()


async def test_guard_expires_without_additional_provider_frames(compiled, engine, trace, spec, fake_clock, make_fake_sonic):
    guard = OutputGuard(stall_phrases=compiled.stall_phrases,
        held_response_max_ms=engine.guard.held_response_max_ms,
        trace=trace, call_id=spec.call_id, task_id=spec.task_id, clock=fake_clock)
    chain = Chain(guard)
    sonic = make_fake_sonic(sink=chain.push)
    async with chain:
        guard.set_state("conversation")
        await sonic.emit_response(SonicResponse(text="I will enroll you in autopay.", audio_ms=200))
        await fake_clock.advance(engine.guard.held_response_max_ms + 1)
        assert trace.of_type("guard_blocked", call_id=spec.call_id)
        assert not chain.tail.tts_audio()


async def test_pending_answer_uses_selected_label(spec, settings, store, gate, trace, fake_clock,
                                                  make_fake_sonic, fake_carrier, monkeypatch):
    from call_engine._support import LiveCallHarness, patch_menu_parser, wait_until
    from waitless import models as m
    patch_menu_parser(monkeypatch)
    configured = spec.model_copy(update={"compiled": spec.compiled.model_copy(update={
        "policy": {"global": {"ask_user": {"max_options": 3, "timeout_sec": 20}}}})})
    sonic = make_fake_sonic()
    harness = LiveCallHarness(monkeypatch=monkeypatch, settings=settings, spec=configured,
        clock=fake_clock, trace=trace, gate=gate, sonic=sonic, carrier=fake_carrier, store=store)
    harness.deps.llm.offer_options = [m.OfferOption(label="Keep the deposit", detail="One-time deposit"),
                                    m.OfferOption(label="Choose autopay", detail="Recurring payments")]
    try:
        await harness.start()
        await harness.connect_line()
        actions = harness.call._actions
        decision = record_decision(trace, make_result("ask_user", call_phase="live_rep"),
            task_id=spec.task_id, call_id=spec.call_id, turn_seq=0)
        await actions.execute(decision)
        choice = harness.call.status().pending_choice
        assert [o.label for o in choice.options] == ["Keep the deposit", "Choose autopay"]
        await harness.call.answer(choice.choice_id, choice.options[0].id)
        assert sonic.sent_texts[-1] == m.SONIC_INSTRUCTION_PREFIX + "The account holder chose Keep the deposit."
    finally:
        await harness.close()


async def test_pending_check_cannot_write_inside_handoff(spec, deps, engine, trace, fake_clock, make_fake_sonic):
    from call_engine._support import MiniCall, handoff_round
    from call_engine.test_said_as_decided import SlowGate
    from waitless import models as m
    gate = SlowGate(trace)
    deps.gate = gate
    announce = SonicResponse(text=spec.compiled.say_templates["handoff.announce"], audio_ms=200)
    async with MiniCall(spec=spec, deps=deps, engine=engine, make_sonic=make_fake_sonic,
                        responses=[announce]) as call:
        say = record_decision(trace, make_result("say", say_text="Keep the deposit."),
            task_id=spec.task_id, call_id=spec.call_id, turn_seq=0)
        await call.actions.execute(say)
        await call.tap.on_provider_text(m.ProviderTextEvent(role="ASSISTANT", stage="FINAL", text="Keep it.",
            stop_reason="END_TURN", t_ms=fake_clock.monotonic_ms()))
        await asyncio.sleep(0)
        handoff = record_decision(trace, handoff_round(), task_id=spec.task_id, call_id=spec.call_id, turn_seq=1)
        await call.run(handoff)
        before = len(trace.events())
        gate.go.set()
        for _ in range(20):
            await asyncio.sleep(0)
        assert len(trace.events()) == before


async def test_ivr_state_blocks_an_already_allowed_response(compiled, engine, trace, spec, fake_clock):
    from pipecat.frames.frames import LLMFullResponseStartFrame, LLMTextFrame, TTSAudioRawFrame
    guard = OutputGuard(stall_phrases=compiled.stall_phrases,
        held_response_max_ms=engine.guard.held_response_max_ms,
        trace=trace, call_id=spec.call_id, task_id=spec.task_id, clock=fake_clock)
    chain = Chain(guard)
    async with chain:
        guard.set_state("conversation")
        await chain.push(LLMFullResponseStartFrame())
        await chain.push(LLMTextFrame(compiled.stall_phrases[0]))
        guard.set_state("ivr")
        await chain.push(TTSAudioRawFrame(audio=bytes(80), sample_rate=engine.audio.pipeline_in_sample_rate, num_channels=1))
    assert not chain.tail.tts_audio()


async def test_old_response_end_does_not_consume_injected_allowance(compiled, engine, trace, spec, fake_clock, make_fake_sonic):
    from pipecat.frames.frames import LLMFullResponseStartFrame, LLMFullResponseEndFrame, LLMTextFrame
    guard = OutputGuard(stall_phrases=compiled.stall_phrases,
        held_response_max_ms=engine.guard.held_response_max_ms,
        trace=trace, call_id=spec.call_id, task_id=spec.task_id, clock=fake_clock)
    chain = Chain(guard)
    sonic = make_fake_sonic(sink=chain.push)
    async with chain:
        guard.set_state("conversation")
        await chain.push(LLMFullResponseStartFrame())
        await chain.push(LLMTextFrame("I will choose autopay."))
        guard.set_state("injected")
        await chain.push(LLMFullResponseEndFrame())
        await sonic.emit_response(SonicResponse(text="Please keep the deposit.", audio_ms=200))
    assert chain.tail.tts_audio()
