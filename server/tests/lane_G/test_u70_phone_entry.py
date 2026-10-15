"""U70-2/3: phone entry keys only the authorized profile digits, without speech."""

import pytest

from fakes import round_answers
from g_support import ACCOUNT_NUMBER, build_kit, profile, speech_turn


PHONE_TEXT = "Please enter your phone number, followed by the pound key."


@pytest.mark.parametrize("raw", [
    "(312) 555-0147", "312-555-0147", "312.555.0147",
    "+1 312 555 0147", "1 (312) 555-0147", "3125550147",
])
async def test_u70_2_phone_entry_keys_profile_digits(
    skills_dir, test_profile, fake_jev, fake_clock, raw,
):
    kit = build_kit(skills_dir, jev=fake_jev, clock=fake_clock,
                    prof=profile(test_profile, phone_number=raw))
    fake_jev.push(round_answers("press_field:phone_number", menu_confidence=0.9,
                               call_phase="ai_agent", stakes=1))
    result = await kit.round(speech_turn(PHONE_TEXT), phase="ai_agent")
    assert (result.intent, result.action, result.digits) == (
        "press_field:phone_number", "press", "3125550147#")
    assert result.say_text is None


async def test_u70_2_phone_digits_never_come_from_the_call(
    skills_dir, test_profile, fake_jev, fake_clock,
):
    kit = build_kit(skills_dir, jev=fake_jev, clock=fake_clock, prof=profile(test_profile))
    fake_jev.push(round_answers("press_field:phone_number", menu_confidence=0.9,
                               call_phase="ai_agent", stakes=1))
    result = await kit.round(speech_turn(
        "Please enter phone number 8475550123, followed by the pound key."), phase="ai_agent")
    assert result.digits == "3125550147#"
    assert "8475550123" not in result.digits
    assert result.say_text is None


@pytest.mark.parametrize("invalid", ["no_digits", "context_mismatch"])
async def test_u70_2_phone_entry_requires_keyable_matching_profile(
    skills_dir, test_profile, fake_jev, fake_clock, invalid,
):
    raw = "n/a" if invalid == "no_digits" else "(312) 555-0147"
    kit = build_kit(skills_dir, jev=fake_jev, clock=fake_clock,
                    prof=profile(test_profile, phone_number=raw))
    turn = speech_turn(PHONE_TEXT)
    ctx = kit.round_ctx(turn, phase="ai_agent")
    if invalid == "context_mismatch":
        ctx.context.allowed_fields["phone_number"] = "(312) 555-0199"
    fake_jev.push(round_answers("press_field:phone_number", menu_confidence=0.9,
                               call_phase="ai_agent", stakes=1))
    result = await kit.gate.decide_round(turn, ctx)
    assert (result.intent, result.action, result.reason_code) == (
        "ask_repeat", "say", "press_field_refused")
    assert result.digits is None


@pytest.mark.parametrize("pick", ["open_answer", "request_handoff", "ask_user", "wait"])
@pytest.mark.parametrize("interrupts", [0, 1])
async def test_u70_3_phone_keypad_request_forces_press(
    skills_dir, test_profile, fake_jev, fake_clock, pick, interrupts,
):
    kit = build_kit(skills_dir, jev=fake_jev, clock=fake_clock, prof=profile(test_profile))
    fake_jev.push(round_answers(pick, needs_user=0.8, stakes=3, call_phase="ai_agent"))
    result = await kit.round(speech_turn(PHONE_TEXT), interrupts_used=interrupts)
    assert (result.intent, result.action, result.digits) == (
        "press_field:phone_number", "press", "3125550147#")
    assert result.say_text is None
    assert len(fake_jev.calls) == len(kit.trace.decisions()) == 1


async def test_u70_3_key_in_phone_without_pound(
    skills_dir, test_profile, fake_jev, fake_clock,
):
    kit = build_kit(skills_dir, jev=fake_jev, clock=fake_clock, prof=profile(test_profile))
    fake_jev.push(round_answers("open_answer", call_phase="ai_agent"))
    result = await kit.round(speech_turn("Key in your phone number."))
    assert (result.intent, result.action, result.digits) == (
        "press_field:phone_number", "press", "3125550147")
    assert result.say_text is None


async def test_u70_3_phone_not_keyable_is_refused_not_spoken(
    skills_dir, test_profile, fake_jev, fake_clock,
):
    kit = build_kit(skills_dir, jev=fake_jev, clock=fake_clock, prof=profile(test_profile))
    # It is already absent before U70-1; discard it without a fixture error in the red run.
    kit.compiled.ivr_map["keypad_fields"] = [
        key for key in kit.compiled.ivr_map["keypad_fields"] if key != "phone_number"]
    turn = speech_turn(PHONE_TEXT)
    ctx = kit.round_ctx(turn)
    fake_jev.push(round_answers("open_answer", call_phase="ai_agent"))
    result = await kit.gate.decide_round(turn, ctx)
    assert (result.intent, result.action, result.reason_code) == (
        "ask_repeat", "say", "press_field_refused")
    assert result.digits is None
    assert "3125550147" not in result.say_text
    assert "555-0147" not in result.say_text


@pytest.mark.parametrize("text,expected", [
    ("What is your phone number?", ("open_answer", "release", None)),
    ("Please enter your account number or phone number, followed by the pound key.",
     ("press_field:account_number", "press", ACCOUNT_NUMBER + "#")),
])
async def test_u70_3_phone_speech_and_account_priority_guards(
    skills_dir, test_profile, fake_jev, fake_clock, text, expected,
):
    kit = build_kit(skills_dir, jev=fake_jev, clock=fake_clock, prof=profile(test_profile))
    fake_jev.push(round_answers("open_answer", call_phase="ai_agent"))
    result = await kit.round(speech_turn(text))
    assert (result.intent, result.action, result.digits) == expected
