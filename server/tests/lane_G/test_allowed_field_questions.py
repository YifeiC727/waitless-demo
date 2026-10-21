"""AC-57: authorized facts override Jev uncertainty, with hard rules intact."""

import pytest
import yaml

from fakes import round_answers
from g_support import ACCOUNT_NUMBER, build_kit, profile, speech_turn


FIELDS = [
    ("address", "What is your service address?"),
    ("account_number", "Can you read me the account number?"),
    ("start_date", "What is the effective date?"),
]


@pytest.mark.parametrize("field,text", FIELDS)
@pytest.mark.parametrize("pick,needs,stakes", [
    ("request_handoff", 0.7, 0),  # forbidden_info branch
    ("request_handoff", 0.1, 0),  # confidence branch
    ("open_answer", 0.7, 0),  # consequential needs_user
    ("open_answer", 0.1, 3),  # consequential stakes
    ("ask_user", 0.1, 0),
    ("wait", 0.1, 0),
])
@pytest.mark.parametrize("phase,interrupts", [("live_rep", 0), ("ai_agent", 1)])
async def test_authorized_fact_is_answered_without_interrupting(
    skills_dir, test_profile, fake_jev, fake_clock, field, text, pick, needs, stakes, phase, interrupts
):
    kit = build_kit(skills_dir, jev=fake_jev, clock=fake_clock, prof=profile(test_profile))
    fake_jev.push(round_answers(pick, needs_user=needs, stakes=stakes, call_phase=phase))
    result = await kit.round(speech_turn(text), phase=phase, interrupts_used=interrupts)
    assert (result.intent, result.action) == ("open_answer", "release")
    assert len(fake_jev.calls) == 1
    assert len(kit.trace.decisions()) == 1


@pytest.mark.parametrize("account,expected", [(ACCOUNT_NUMBER, "press_field:account_number"),
                                             ("LE-ACCT-2048", "ask_repeat")])
async def test_field_entry_still_checks_digits_under_jev_uncertainty(
    skills_dir, test_profile, fake_jev, fake_clock, account, expected
):
    kit = build_kit(skills_dir, jev=fake_jev, clock=fake_clock,
                    prof=profile(test_profile, account=account))
    fake_jev.push(round_answers("press_field:account_number", needs_user=0.9, stakes=3, call_phase="live_rep"))
    result = await kit.round(speech_turn("Enter your account number followed by the pound key."),
                             interrupts_used=1)
    assert result.intent == expected
    if result.action == "press":
        assert result.digits == ACCOUNT_NUMBER + "#"


async def test_unrelated_field_entry_falls_back_to_open_answer(
    skills_dir, test_profile, fake_jev, fake_clock
):
    kit = build_kit(skills_dir, jev=fake_jev, clock=fake_clock, prof=profile(test_profile))
    fake_jev.push(round_answers("press_field:account_number", needs_user=0.7, stakes=3, call_phase="live_rep"))
    result = await kit.round(speech_turn("What is your service address?"))
    assert (result.intent, result.action) == ("open_answer", "release")


async def test_field_entry_rejects_a_context_value_mismatch(
    skills_dir, test_profile, fake_jev, fake_clock
):
    kit = build_kit(skills_dir, jev=fake_jev, clock=fake_clock, prof=profile(test_profile))
    turn = speech_turn("Enter your account number.")
    ctx = kit.round_ctx(turn)
    ctx.context.allowed_fields["account_number"] = "99887766"
    fake_jev.push(round_answers("press_field:account_number", needs_user=0.7, stakes=3,
                               call_phase="live_rep"))
    result = await kit.gate.decide_round(turn, ctx)
    assert (result.intent, result.action) == ("ask_repeat", "say")
    assert result.digits is None


@pytest.mark.parametrize("marker", ["$25", "fee", "deposit", "autopay", "contract", "cancel"])
async def test_consequential_field_question_still_obeys_budget(
    skills_dir, test_profile, fake_jev, fake_clock, marker
):
    kit = build_kit(skills_dir, jev=fake_jev, clock=fake_clock, prof=profile(test_profile))
    kit.compiled.task_card.precall_decisions.clear()
    fake_jev.push(round_answers("request_handoff", needs_user=0.7, stakes=3,
                               call_phase="live_rep"))
    result = await kit.round(speech_turn(f"What is your service address? Please agree to {marker}."),
                             interrupts_used=1)
    assert (result.action, result.reason_code) == ("end", "budget_exhausted")
    if marker == "autopay":
        questions = yaml.safe_load(
            (skills_dir / "company-lakeshore-energy/references/pre-call-questions.yaml").read_text()
        )["questions"]
        deposit = next(q for q in questions if q["question_id"] == "deposit_consent")
        assert "autopay" not in [option["id"] for option in deposit["options"]]


@pytest.mark.parametrize("suffix,expected", [
    (" Am I speaking with the account holder?", "state_ai_identity"),
    (" Also your social security number?", "request_handoff"),
    (" Also your card number?", "request_handoff"),
    (" Also your date of birth?", "request_handoff"),
    (" There is a fee.", "ask_user"),
    (" There is a contract.", "ask_user"),
])
async def test_higher_priority_rules_still_apply(
    skills_dir, test_profile, fake_jev, fake_clock, suffix, expected
):
    kit = build_kit(skills_dir, jev=fake_jev, clock=fake_clock, prof=profile(test_profile))
    fake_jev.push(round_answers("open_answer", needs_user=0.7, stakes=3, call_phase="live_rep"))
    result = await kit.round(speech_turn("What is your service address?" + suffix))
    assert result.intent == expected


@pytest.mark.parametrize("field,text", FIELDS)
async def test_field_without_authorized_value_is_not_exempt(
    skills_dir, test_profile, fake_jev, fake_clock, field, text
):
    kit = build_kit(skills_dir, jev=fake_jev, clock=fake_clock, prof=profile(test_profile))
    if field == "start_date":
        kit.compiled.task_card.effective_date = None
    else:
        kit.compiled.allowed_fields.pop(field)
    fake_jev.push(round_answers("request_handoff", needs_user=0.7, call_phase="live_rep"))
    result = await kit.round(speech_turn(text))
    assert result.intent == "request_handoff"


@pytest.mark.parametrize("phase,expected", [("live_rep", "request_handoff"),
                                            ("ai_agent", "ask_for_representative")])
async def test_ordinary_handoff_question_keeps_existing_behavior(
    skills_dir, test_profile, fake_jev, fake_clock, phase, expected
):
    kit = build_kit(skills_dir, jev=fake_jev, clock=fake_clock, prof=profile(test_profile))
    fake_jev.push(round_answers("request_handoff", needs_user=0.7, call_phase=phase))
    result = await kit.round(speech_turn("Can you help with verification?"), phase=phase)
    assert result.intent == expected
