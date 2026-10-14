"""G-F5: keypad requests must key the authorized value instead of speaking it."""

import pytest

from fakes import round_answers
from g_support import ACCOUNT_NUMBER, KEYPAD_TEXT, build_kit, profile, speech_turn


@pytest.mark.parametrize("text,pick", [
    (KEYPAD_TEXT, "open_answer"),
    ("Press your account number, followed by hash.", "open_answer"),
    ("Key in your account number.", "open_answer"),
    ("Type in your account number.", "open_answer"),
    ("Provide your account number using the keypad.", "open_answer"),
    ("Can you enter your account number, followed by pound?", "request_handoff"),
    ("Could you key in your account number?", "ask_user"),
    ("Would you type in your account number?", "wait"),
])
@pytest.mark.parametrize("interrupts", [0, 1])
async def test_keypad_open_answer_and_question_rewrites_press_profile_digits(
    skills_dir, test_profile, fake_jev, fake_clock, text, pick, interrupts
):
    kit = build_kit(skills_dir, jev=fake_jev, clock=fake_clock, prof=profile(test_profile))
    fake_jev.push(round_answers(pick, needs_user=0.8, stakes=3, call_phase="live_rep"))
    result = await kit.round(speech_turn(text), interrupts_used=interrupts)
    suffix = "#" if "pound" in text.lower() or "hash" in text.lower() else ""
    assert (result.intent, result.action, result.digits) == (
        "press_field:account_number", "press", ACCOUNT_NUMBER + suffix
    )
    assert result.say_text is None
    assert len(fake_jev.calls) == len(kit.trace.decisions()) == 1


@pytest.mark.parametrize("invalid", ["missing", "nonnumeric", "company_disallows", "context_mismatch"])
async def test_forced_entry_still_requires_matching_authorized_keypad_value(
    skills_dir, test_profile, fake_jev, fake_clock, invalid
):
    account = None if invalid == "missing" else "LE-ACCT-2048" if invalid == "nonnumeric" else ACCOUNT_NUMBER
    kit = build_kit(skills_dir, jev=fake_jev, clock=fake_clock,
                    prof=profile(test_profile, account=account))
    if invalid == "company_disallows":
        kit.compiled.ivr_map["keypad_fields"].remove("account_number")
    turn = speech_turn(KEYPAD_TEXT)
    ctx = kit.round_ctx(turn)
    if invalid == "context_mismatch":
        ctx.context.allowed_fields["account_number"] = "99887766"
    fake_jev.push(round_answers("open_answer", call_phase="live_rep"))
    result = await kit.gate.decide_round(turn, ctx)
    assert (result.intent, result.action, result.reason_code) == ("ask_repeat", "say", "press_field_refused")
    assert result.digits is None
    assert ACCOUNT_NUMBER not in result.say_text
    assert len(fake_jev.calls) == len(kit.trace.decisions()) == 1


@pytest.mark.parametrize("text,pick", [
    ("What is your account number?", "open_answer"),
    ("Please enter your service address.", "open_answer"),
    ("Please enter your account number, followed by pound.", "wait"),
])
async def test_other_fields_and_statement_choices_keep_existing_behavior(
    skills_dir, test_profile, fake_jev, fake_clock, text, pick
):
    kit = build_kit(skills_dir, jev=fake_jev, clock=fake_clock, prof=profile(test_profile))
    fake_jev.push(round_answers(pick, call_phase="live_rep"))
    result = await kit.round(speech_turn(text))
    assert (result.intent, result.action) == (pick, "release" if pick == "open_answer" else "wait")
    assert result.digits is None


async def test_forced_entry_does_not_use_digits_from_company_text(
    skills_dir, test_profile, fake_jev, fake_clock
):
    kit = build_kit(skills_dir, jev=fake_jev, clock=fake_clock, prof=profile(test_profile))
    fake_jev.push(round_answers("open_answer", call_phase="live_rep"))
    result = await kit.round(speech_turn("Please enter account number 99887766 followed by pound."))
    assert (result.intent, result.action, result.digits) == ("press_field:account_number", "press", ACCOUNT_NUMBER + "#")
    assert result.say_text is None
