"""I2E-5: injected content is the whole reply; stall only before instruction."""

import pytest

from g_support import profile, standing_rules, task_card
from waitless.models import SONIC_INSTRUCTION_PREFIX
from waitless.skills.compiler import compile_call
from waitless.skills.loader import load_skills


@pytest.fixture
def compiled(skills_dir, test_profile):
    return compile_call(task_card(), load_skills(skills_dir),
                        profile(test_profile), standing_rules(), [])


def test_r12_instruction_is_immediate_whole_reply_without_extra_words(compiled):
    injection = compiled.sonic_prompt.split("7. Instructions", 1)[1].split("8. Honesty", 1)[0]
    assert SONIC_INSTRUCTION_PREFIX.strip() in injection
    assert "your next reply is exactly its authorized content" in injection
    assert "said right away as your whole reply" in injection
    assert "Do not add a stall phrase, greeting, or any other words before or after it" in injection
    assert "do not add commitments" in injection


def test_r12_stall_is_whole_reply_only_before_instruction(compiled):
    stall = compiled.sonic_prompt.split("5. Choices and verification", 1)[1].split("6. Keypad", 1)[0]
    assert f"when no {SONIC_INSTRUCTION_PREFIX.strip()} message has told you what to say yet" in stall
    assert "as your whole reply" in stall
    for topic in ("choice", "agreement", "confirmation", "identity question",
                  "information absent from these facts"):
        assert topic in stall
    assert compiled.stall_phrases
    for phrase in compiled.stall_phrases:
        assert f"- {phrase}" in stall.splitlines()
