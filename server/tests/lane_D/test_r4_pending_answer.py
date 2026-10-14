"""D-F5: fill both pending-answer placeholders and restore the active call phase."""
import pytest
from call_engine.conftest import compiled, engine, gate, line_number, settings, spec, store, trace
from call_engine._support import LiveCallHarness, make_result, record_decision
from waitless import models as m


@pytest.mark.parametrize("phase,detail", [("live_rep", "One-time deposit"), ("ai_agent", "")])
async def test_answer_formats_option_detail_and_restores_phase(phase, detail, spec, settings, store,
        gate, trace, fake_clock, make_fake_sonic, fake_carrier, monkeypatch):
    configured = spec.model_copy(update={"compiled": spec.compiled.model_copy(update={
        "say_templates": {**spec.compiled.say_templates,
            "pending.answer": "The account holder chose: {option_label}. {option_detail}"},
        "policy": {"global": {"ask_user": {"max_options": 3, "timeout_sec": 20}}}})})
    sonic = make_fake_sonic()
    h = LiveCallHarness(monkeypatch=monkeypatch, settings=settings, spec=configured, clock=fake_clock,
        trace=trace, gate=gate, sonic=sonic, carrier=fake_carrier, store=store)
    h.deps.llm.offer_options = [m.OfferOption(label="Keep the deposit", detail=detail)]
    try:
        await h.start()
        await h.connect_line()
        decision = record_decision(trace, make_result("ask_user", call_phase=phase),
                                  task_id=spec.task_id, call_id=spec.call_id, turn_seq=0)
        await h.call._actions.execute(decision)
        choice = h.call.status().pending_choice
        answer = await h.call.answer(choice.choice_id, choice.options[0].id)
        assert answer.status == "choice_recorded"
        assert sonic.sent_texts[-1] == m.SONIC_INSTRUCTION_PREFIX + f"The account holder chose: Keep the deposit. {detail}"
        assert h.call.status().phase == m.CALL_PHASE_FROM_JEV[phase]
        assert h.call.status().phase != "resuming"
    finally:
        await h.close()
