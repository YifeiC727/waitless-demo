"""G-F8: only the field's sentence requests a response; match whole words."""

import pytest

from fakes import round_answers
from g_support import ACCOUNT_NUMBER, build_kit, profile, speech_turn


@pytest.mark.parametrize("text,pick,expected,action", [
    ("Please provide your service address.", "request_handoff", "open_answer", "release"),
    ("Kindly tell me your service address.", "ask_user", "open_answer", "release"),
    ("Kindly tell us your service address.", "ask_user", "open_answer", "release"),
    ("Please give your service address.", "request_handoff", "open_answer", "release"),
    ("Tell me the account number on the bill.", "wait", "open_answer", "release"),
    ("I need your service address.", "request_handoff", "open_answer", "release"),
    ("I will need your service address.", "wait", "open_answer", "release"),
    ("May I have your account number.", "ask_user", "open_answer", "release"),
    ("Please enter your account number, followed by the pound key.", "request_handoff", "press_field:account_number", "press"),
    ("Please enter your account number, followed by the pound key.", "ask_user", "press_field:account_number", "press"),
    ("Your start date is set and your confirmation number is LE-12345. Is there anything else?", "confirm_and_end", "confirm_and_end", "end"),
    ("Okay. What is the service address", "wait", "open_answer", "release"),
    ("Okay! WHAT IS THE SERVICE ADDRESS", "wait", "open_answer", "release"),
    ("Please hold while I check with our service center.", "wait", "wait", "wait"),
    ("What is your account number? It is printed near the center of your bill.", "open_answer", "open_answer", "release"),
    ("What is your account number? I entered it earlier.", "open_answer", "open_answer", "release"),
    ("What is your account number? Press hold while I check.", "open_answer", "open_answer", "release"),
    ("Your account number is recorded. Can you enter the queue?", "wait", "wait", "wait"),
    ("The service addresses are saved. Is there anything else?", "confirm_and_end", "confirm_and_end", "end"),
])
@pytest.mark.parametrize("interrupts", [0, 1])
async def test_field_sentence_controls_rewrite_and_keypad(
    skills_dir, test_profile, fake_jev, fake_clock, text, pick, expected, action, interrupts
):
    kit = build_kit(skills_dir, jev=fake_jev, clock=fake_clock, prof=profile(test_profile))
    fake_jev.push(round_answers(pick, call_phase="ended" if action == "end" else "live_rep"))
    result = await kit.round(speech_turn(text), interrupts_used=interrupts)
    assert (result.intent, result.action) == (expected, action)
    assert result.digits == (ACCOUNT_NUMBER + "#" if action == "press" else None)
    assert len(fake_jev.calls) == len(kit.trace.decisions()) == 1


@pytest.mark.parametrize("account", [None, "LE-ACCT-2048"])
@pytest.mark.parametrize("pick", ["request_handoff", "ask_user"])
async def test_keypad_request_without_usable_account_is_refused(
    skills_dir, test_profile, fake_jev, fake_clock, account, pick
):
    kit = build_kit(skills_dir, jev=fake_jev, clock=fake_clock,
                    prof=profile(test_profile, account=account))
    fake_jev.push(round_answers(pick, call_phase="live_rep"))
    result = await kit.round(speech_turn("Please enter your account number, followed by the pound key."))
    assert (result.intent, result.action, result.reason_code) == ("ask_repeat", "say", "press_field_refused")
    assert result.digits is None
