"""I2A-3: distinguish a keypad menu from a later conversational greeting."""

from g_support import point_questions


def test_call_phase_uses_last_utterance_and_requires_keypad_options(skills_dir):
    instructions = point_questions(skills_dir, "call_round")["call_phase"]["instructions"]
    assert "last company utterance" in instructions
    assert "keypad" in instructions
    assert "Choose ivr_menu only when" in instructions
    assert "Menus earlier in the transcript do not make a later conversational utterance a menu." in instructions
    assert "ai_agent when it comes from an automated assistant" in instructions
    assert "live_rep when it comes from a person" in instructions


def test_call_phase_criteria_keep_other_five_descriptions(skills_dir):
    criteria = point_questions(skills_dir, "call_round")["call_phase"]["criteria"]
    assert "keypad" in criteria["ivr_menu"]
    assert {key: value for key, value in criteria.items() if key != "ivr_menu"} == {
        "hold": "Hold music or a wait message",
        "ai_agent": "An automated virtual agent asking questions",
        "live_rep": "A human representative speaking",
        "identity_check": "Someone asking to verify the account holder's identity",
        "ended": "The call has ended or the other side said goodbye",
    }


def test_greeting_with_open_question_chooses_open_answer(skills_dir):
    instructions = point_questions(skills_dir, "call_round")["menu_choice"]["instructions_conversation"]
    assert "if the greeting also asks how it can help or what the caller needs, choose open_answer" in instructions
    for cue in ("last company utterance", "greeting", "hold", "transfer", "closing", "wait", "confirm_and_end"):
        assert cue in instructions
