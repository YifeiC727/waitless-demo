"""I2F-1: clarify holder handoff while preserving candidates and identity priority."""

import pytest

from g_support import (
    IDENTITY_TEXT,
    build_kit,
    point_questions,
    profile,
    read_yaml,
    round_jev_answers,
    speech_turn,
    standing_rules,
    task_card,
)
from waitless.decision.gate import round_candidates
from waitless.skills.compiler import compile_call
from waitless.skills.loader import load_skills


HOLDER_TEXT = (
    "i need the account holder on this same call to verify their identity. "
    "please bring them onto the line."
)
HANDOFF_DESCRIPTION = (
    "Bring the account holder onto this same call: the company asks for the account "
    "holder to join or come on the line, or for verification only the account holder "
    "can give; not for a question about who is speaking"
)
OTHER_DESCRIPTIONS = {
    "open_answer": "Answer an open-ended question using authorized facts",
    "decline_offer": "Decline surveys or marketing offers",
    "state_ai_identity": "Truthfully disclose AI identity",
    "ask_user": "Ask the account holder to decide",
    "confirm_and_end": "Close after task confirmation",
    "ask_repeat": "Ask for repetition or clarification",
    "wait": "Wait while holding or transferring",
    "press_field:account_number": "Enter the authorized account_number",
    "press_field:phone_number": "Enter the authorized phone_number",
    "answer_precall:deposit_or_autopay": "Pay a deposit or enroll in autopay?",
}
CANDIDATE_IDS = [
    "open_answer", "decline_offer", "state_ai_identity", "request_handoff",
    "ask_user", "confirm_and_end", "ask_repeat", "wait",
    "press_field:account_number", "press_field:phone_number", "answer_precall:deposit_or_autopay",
]
CONVERSATION_INSTRUCTIONS = (
    "Choose how Waitless should respond to the last company utterance (the "
    "final transcript line), given the task goal and rules. For greetings, "
    "hold or queue messages, and transfer notices choose wait, but if the "
    "greeting also asks how it can help or what the caller needs, choose "
    "open_answer. When a closing statement already gives a confirmation "
    "number, choose confirm_and_end."
)


@pytest.fixture
def compiled(skills_dir, test_profile):
    return compile_call(task_card(), load_skills(skills_dir),
                        profile(test_profile), standing_rules(), [])


def test_request_handoff_description_matches_d2_exactly(compiled):
    candidates = round_candidates(speech_turn(HOLDER_TEXT), compiled)
    handoff = next(c for c in candidates if c.id == "request_handoff")
    assert handoff.description == HANDOFF_DESCRIPTION


def test_other_candidates_and_conversation_instructions_are_unchanged(compiled, skills_dir):
    candidates = round_candidates(speech_turn(HOLDER_TEXT), compiled)
    assert [c.id for c in candidates] == CANDIDATE_IDS
    assert {c.id: c.description for c in candidates if c.id != "request_handoff"} == OTHER_DESCRIPTIONS
    assert [i["action"] for i in compiled.intents] == [
        "release", "say", "say", "handoff", "ask_user", "end", "say", "wait", "press", "press", "say",
    ]
    intents = read_yaml(skills_dir / "decide-option" / "references" / "intents.yaml")
    assert intents["allowed_fields"] == [
        "name", "phone_number", "email", "address", "zip", "account_number",
    ]
    questions = point_questions(skills_dir, "call_round")
    assert questions["menu_choice"]["instructions_conversation"] == CONVERSATION_INSTRUCTIONS


def test_fake_jev_holder_hands_off_but_identity_still_takes_priority(
    skills_dir, test_profile, fake_jev, fake_clock,
):
    kit = build_kit(skills_dir, jev=fake_jev, clock=fake_clock, prof=profile(test_profile))
    for text, expected in (
        (HOLDER_TEXT, ("request_handoff", "handoff", "call_round.forbidden_info")),
        (IDENTITY_TEXT, ("state_ai_identity", "say", "call_round.identity")),
    ):
        request = kit.round_request(speech_turn(text), phase="live_rep")
        answers = round_jev_answers(
            [c.id for c in request.candidates], "request_handoff", menu_confidence=0.93,
            call_phase="identity_check", needs_user=0.84, stakes=0.02,
        )
        outcome = kit.policy.evaluate_round(request, answers, kit.compiled)
        assert (outcome.intent, outcome.action, outcome.policy_rule) == expected
