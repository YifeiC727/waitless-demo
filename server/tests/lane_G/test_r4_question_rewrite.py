"""G-F4: rewrite authorized field questions, preserve Jev's statements."""

import pytest

from fakes import round_answers
from g_support import build_kit, profile, speech_turn


@pytest.mark.parametrize("text,pick,action", [
    ("Thanks, I have the service address. Please hold while I check availability.", "wait", "wait"),
    ("Your start date is set and your confirmation number is LE-12345. Goodbye.", "confirm_and_end", "end"),
    ("The account number is recorded. Please hold.", "wait", "wait"),
    ("The street address is saved. Goodbye.", "confirm_and_end", "say"),
    ("Whatever your service address is, please hold.", "wait", "wait"),
])
@pytest.mark.parametrize("interrupts", [0, 1])
async def test_field_statement_keeps_jev_choice_and_confirmation_check(
    skills_dir, test_profile, fake_jev, fake_clock, text, pick, action, interrupts
):
    kit = build_kit(skills_dir, jev=fake_jev, clock=fake_clock, prof=profile(test_profile))
    fake_jev.push(round_answers(pick, call_phase="live_rep"))
    result = await kit.round(speech_turn(text), interrupts_used=interrupts)
    expected = "ask_repeat" if action == "say" else pick
    assert (result.intent, result.action) == (expected, action)
    if expected == "ask_repeat":
        assert result.reason_code == "end_without_confirmation"
    assert len(fake_jev.calls) == len(kit.trace.decisions()) == 1


@pytest.mark.parametrize("text", [
    "What is your service address?",
    "Can you read me the account number?",
    "What is the effective date?",
    "  Could you read me the service address",
    "Would you give me the street address",
    "When should service begin",
    "Which service address should I use",
    "Where is your service address",
    "May I have your account number",
    "I need your service address?",
    "On what date should service start?",
])
@pytest.mark.parametrize("pick,needs", [("wait", 0.1), ("request_handoff", 0.8),
                                       ("ask_user", 0.8), ("confirm_and_end", 0.1)])
async def test_authorized_question_rewrites_other_intents(
    skills_dir, test_profile, fake_jev, fake_clock, text, pick, needs
):
    kit = build_kit(skills_dir, jev=fake_jev, clock=fake_clock, prof=profile(test_profile))
    fake_jev.push(round_answers(pick, needs_user=needs, call_phase="live_rep"))
    result = await kit.round(speech_turn(text), interrupts_used=1)
    assert (result.intent, result.action) == ("open_answer", "release")
    assert result.digits is None
    assert len(fake_jev.calls) == len(kit.trace.decisions()) == 1


async def test_question_with_wrong_field_entry_is_rewritten(
    skills_dir, test_profile, fake_jev, fake_clock
):
    kit = build_kit(skills_dir, jev=fake_jev, clock=fake_clock, prof=profile(test_profile))
    fake_jev.push(round_answers("press_field:account_number", call_phase="live_rep"))
    result = await kit.round(speech_turn("What is your service address?"))
    assert (result.intent, result.action) == ("open_answer", "release")
    assert result.digits is None
    assert len(fake_jev.calls) == len(kit.trace.decisions()) == 1
