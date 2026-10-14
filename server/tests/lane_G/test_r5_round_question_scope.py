"""G-F6: score the response to the last company utterance, not the task."""

import pytest

from g_support import point_questions


@pytest.mark.parametrize("qid,key", [
    ("stakes", "instructions"),
    ("needs_user", "instructions"),
    ("menu_choice", "instructions_conversation"),
])
def test_round_questions_limit_risk_to_last_utterance(skills_dir, qid, key):
    instructions = point_questions(skills_dir, "call_round")[qid][key].lower()
    assert "last company utterance" in instructions
    for cue in ("greeting", "hold", "transfer", "closing"):
        assert cue in instructions
    if qid == "menu_choice":
        assert "wait" in instructions and "confirm_and_end" in instructions
    if qid == "stakes":
        assert "overall task" in instructions
        assert "deposits" in instructions and "autopay" in instructions


def test_stakes_scale_keeps_the_frozen_grade_text(skills_dir):
    assert point_questions(skills_dir, "call_round")["stakes"]["criteria"] == [
        "0: no consequence", "1: minor inconvenience",
        "2: changes the account or service terms", "3: involves money or a contract",
    ]
