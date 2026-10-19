"""U84-2/3: use only safe declared defaults after the single interruption."""

import pytest
import yaml

from fakes import round_answers
from g_support import (
    DEPOSIT_LABEL,
    SURGE_TEXT,
    build_kit,
    profile,
    speech_turn,
    task_card,
)
from waitless.models import PrecallDecision


BASELINE_DEPOSIT = "Verification is complete. Would the account holder prefer a refundable deposit or autopay?"
DEFAULT_SAY = "Please use the default option for now: Pay a security deposit. The account holder can change it later."
DATE_TEXT = "Your requested start date is unavailable. Which date would you like instead?"


def _deposit_consent_card():
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
        default=None, selected="ask_me", needs_user=False,
    ))
    return card


@pytest.mark.parametrize("pick", ["ask_user", "open_answer", "request_handoff", "confirm_and_end"])
async def test_u84_deposit_after_budget_requires_follow_up_without_a_default(
    skills_dir, test_profile, fake_jev, fake_clock, pick
):
    kit = build_kit(skills_dir, jev=fake_jev, clock=fake_clock, prof=profile(test_profile),
                    card=task_card(precall=False))
    fake_jev.push(round_answers(pick, menu_confidence=0.95, call_phase="live_rep", needs_user=0.9, stakes=3))
    result = await kit.round(speech_turn(BASELINE_DEPOSIT), interrupts_used=1)
    assert (result.intent, result.action, result.reason_code) == (
        "end_needs_follow_up", "end", "budget_exhausted"
    )
    assert result.policy_rule == "call_round.budget"
    assert result.say_text == "Thank you. The account holder will follow up if needed. Goodbye."
    assert result.pending is None
    assert len(fake_jev.calls) == len(kit.trace.decisions()) == 1


def test_u84_answer_default_template(skills_dir):
    doc = yaml.safe_load((skills_dir / "decide-option/references/say-templates.yaml").read_text())
    assert doc["say_templates"]["answer_default"].format(option_label=DEPOSIT_LABEL) == DEFAULT_SAY


async def test_u84_deposit_with_budget_left_asks_the_user(
    skills_dir, test_profile, fake_jev, fake_clock
):
    kit = build_kit(skills_dir, jev=fake_jev, clock=fake_clock, prof=profile(test_profile),
                    card=task_card(precall=False))
    fake_jev.push(round_answers("open_answer", menu_confidence=0.95, call_phase="live_rep", needs_user=0.1, stakes=3))
    result = await kit.round(speech_turn(BASELINE_DEPOSIT), interrupts_used=0)
    assert (result.intent, result.action) == ("ask_user", "ask_user")


@pytest.mark.parametrize("interrupts", [0, 1])
async def test_u84_user_choice_on_the_card_still_wins(
    skills_dir, test_profile, fake_jev, fake_clock, interrupts
):
    kit = build_kit(skills_dir, jev=fake_jev, clock=fake_clock, prof=profile(test_profile),
                    card=_deposit_consent_card())
    fake_jev.push(round_answers("ask_user", menu_confidence=0.95, call_phase="live_rep", needs_user=0.9, stakes=3))
    result = await kit.round(speech_turn("Verification is complete. Are you willing to pay a refundable deposit?"),
                             interrupts_used=interrupts)
    assert (result.intent, result.action, result.reason_code) == (
        "answer_precall:deposit_consent", "say", "answer_precall"
    )
    assert result.say_text == "The account holder chose: No, I will decide on the call."


async def test_u84_other_money_question_after_budget_still_ends(
    skills_dir, test_profile, fake_jev, fake_clock
):
    kit = build_kit(skills_dir, jev=fake_jev, clock=fake_clock, prof=profile(test_profile),
                    card=task_card(precall=False))
    fake_jev.push(round_answers("ask_user", call_phase="live_rep", needs_user=0.9, stakes=3))
    result = await kit.round(speech_turn(SURGE_TEXT), interrupts_used=1)
    assert (result.action, result.reason_code) == ("end", "budget_exhausted")


def test_u84_lakeshore_question_defaults(skills_dir):
    doc = yaml.safe_load((skills_dir / "company-lakeshore-energy/references/pre-call-questions.yaml").read_text())
    questions = doc["questions"]
    assert [q["question_id"] for q in questions] == ["deposit_consent", "start_date"]
    assert [[o["id"] for o in q["options"]] for q in questions] == [
        ["approve", "ask_me"], ["ask", "next_available"]
    ]
    assert [q["tags"] for q in questions] == [["deposit"], ["start date", "available date"]]
    assert questions[0]["default"] is None
    assert questions[1]["default"] is None


async def test_u84_date_change_after_budget_still_ends(
    skills_dir, test_profile, fake_jev, fake_clock
):
    kit = build_kit(skills_dir, jev=fake_jev, clock=fake_clock, prof=profile(test_profile),
                    card=task_card(precall=False))
    fake_jev.push(round_answers("open_answer", call_phase="live_rep", needs_user=0.9, stakes=2))
    result = await kit.round(speech_turn(DATE_TEXT), interrupts_used=1)
    assert (result.intent, result.action, result.reason_code) == (
        "end_needs_follow_up", "end", "budget_exhausted"
    )
