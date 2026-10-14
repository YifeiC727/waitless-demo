"""Authorization to state a fact does not authorize changing service details."""

import pytest

from fakes import round_answers
from g_support import build_kit, profile, speech_turn, task_card
from waitless.models import Option, PrecallDecision


@pytest.mark.parametrize("text", [
    "Your requested start date is unavailable. Which date would you like instead?",
    "Can we change the start date to the next available date?",
    "Should we change your service address?",
    "May I update the account number on this service?",
])
@pytest.mark.parametrize("interrupts,expected", [(0, "ask_user"), (1, "end_needs_follow_up")])
async def test_field_change_obeys_explicit_choice_and_budget(
    skills_dir, test_profile, fake_jev, fake_clock, text, interrupts, expected
):
    kit = build_kit(skills_dir, jev=fake_jev, clock=fake_clock, prof=profile(test_profile),
                    card=task_card(precall=False))
    fake_jev.push(round_answers("open_answer", call_phase="live_rep", needs_user=0.9, stakes=2))
    result = await kit.round(speech_turn(text), interrupts_used=interrupts)
    assert result.intent == expected
    assert result.action == ("ask_user" if interrupts == 0 else "end")
    assert len(fake_jev.calls) == len(kit.trace.decisions()) == 1


@pytest.mark.parametrize("selected", ["ask", "next_available"])
async def test_date_change_uses_confirmed_precall_choice_even_after_budget(
    skills_dir, test_profile, fake_jev, fake_clock, selected
):
    card = task_card(precall=False)
    card.precall_decisions = [PrecallDecision(
        question_id="start_date", question="What if the requested start date is unavailable?",
        options=[Option(id="ask", label="Ask me before changing the start date", detail=""),
                 Option(id="next_available", label="Choose the next available date", detail="")],
        default="ask", selected=selected, needs_user=False,
    )]
    kit = build_kit(skills_dir, jev=fake_jev, clock=fake_clock, prof=profile(test_profile), card=card)
    fake_jev.push(round_answers("open_answer", call_phase="live_rep", needs_user=0.9, stakes=2))
    result = await kit.round(speech_turn("The requested start date is unavailable. Can we change it?"),
                             interrupts_used=1)
    assert (result.intent, result.action) == ("answer_precall:start_date", "say")
    label = next(option.label for option in card.precall_decisions[0].options if option.id == selected)
    assert label in result.say_text
