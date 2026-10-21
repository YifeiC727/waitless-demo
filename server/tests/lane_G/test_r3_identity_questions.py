"""G-F1: mentioning the account holder does not itself ask identity."""

import pytest

from fakes import round_answers
from g_support import build_kit, profile, speech_turn, task_card
from waitless.models import PrecallDecision


@pytest.mark.parametrize("text", [
    "Am I speaking with the account holder?",
    "Are you the account holder?",
])
@pytest.mark.parametrize("interrupts", [0, 1])
async def test_identity_questions_override_jev_and_budget(
    skills_dir, test_profile, fake_jev, fake_clock, text, interrupts
):
    kit = build_kit(skills_dir, jev=fake_jev, clock=fake_clock, prof=profile(test_profile))
    fake_jev.push(round_answers("wait", call_phase="live_rep"))
    result = await kit.round(speech_turn(text), interrupts_used=interrupts)
    assert (result.intent, result.action) == ("state_ai_identity", "say")
    assert len(fake_jev.calls) == len(kit.trace.decisions()) == 1


@pytest.mark.parametrize("phase", ["live_rep", "identity_check"])
async def test_baseline_holder_request_hands_off(
    skills_dir, test_profile, fake_jev, fake_clock, phase
):
    kit = build_kit(skills_dir, jev=fake_jev, clock=fake_clock, prof=profile(test_profile))
    fake_jev.push(round_answers("request_handoff", needs_user=0.8, call_phase=phase))
    result = await kit.round(speech_turn(
        "I need the account holder on this same call to verify their identity. Please bring them onto the line."
    ), phase="live_rep")
    assert (result.intent, result.action) == ("request_handoff", "handoff")
    assert len(fake_jev.calls) == len(kit.trace.decisions()) == 1


async def test_baseline_deposit_uses_confirmed_choice(
    skills_dir, test_profile, fake_jev, fake_clock
):
    card = task_card(precall=False)
    card.precall_decisions.append(PrecallDecision(
        question_id="deposit_consent",
        question=("A refundable deposit may be required. The account holder will confirm it on the call. "
                  "Should Waitless tell the company you are willing to pay it?"),
        options=[
            {"id": "approve", "label": "Yes, I am willing to pay a refundable deposit",
             "detail": "You still confirm it yourself on the call"},
            {"id": "ask_me", "label": "No, I will decide on the call",
             "detail": "Waitless does not agree to a deposit for you"},
        ],
        default=None, selected="approve", needs_user=False,
    ))
    kit = build_kit(skills_dir, jev=fake_jev, clock=fake_clock, prof=profile(test_profile), card=card)
    fake_jev.push(round_answers("open_answer", needs_user=0.8, stakes=3, call_phase="live_rep"))
    result = await kit.round(speech_turn(
        "Verification is complete. Is the account holder willing to pay a refundable deposit?"
    ))
    assert (result.intent, result.action) == ("answer_precall:deposit_consent", "say")
    selected = kit.compiled.task_card.precall_decisions[0]
    assert next(o.label for o in selected.options if o.id == selected.selected) in result.say_text
    assert len(fake_jev.calls) == len(kit.trace.decisions()) == 1


@pytest.mark.parametrize("phase,expected,action", [
    ("live_rep", "request_handoff", "handoff"),
    ("ai_agent", "ask_for_representative", "say"),
])
async def test_baseline_birth_date_uses_forbidden_rule(
    skills_dir, test_profile, fake_jev, fake_clock, phase, expected, action
):
    kit = build_kit(skills_dir, jev=fake_jev, clock=fake_clock, prof=profile(test_profile))
    fake_jev.push(round_answers("open_answer", call_phase=phase))
    result = await kit.round(speech_turn("For security, I need the account holder's date of birth."), phase=phase)
    assert (result.intent, result.action) == (expected, action)
    assert result.policy_rule == "call_round.forbidden_info"
    assert len(fake_jev.calls) == len(kit.trace.decisions()) == 1
