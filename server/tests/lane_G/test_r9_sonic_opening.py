"""I2B-4: first speech discloses AI identity; choices and keypad stay gated."""

import pytest

from g_support import DEPOSIT_LABEL, profile, standing_rules, task_card
from waitless.models import SONIC_INSTRUCTION_PREFIX
from waitless.skills.compiler import compile_call
from waitless.skills.loader import load_skills


@pytest.fixture
def compiled(skills_dir, test_profile):
    return compile_call(task_card(), load_skills(skills_dir),
                        profile(test_profile), standing_rules(), [])


def test_first_speech_starts_with_ai_identity(compiled, test_profile):
    prompt = compiled.sonic_prompt
    identity = prompt.split("2. Task")[0]
    assert "first time you speak" in identity.lower()
    assert "begin by saying" in identity.lower()
    assert "AI assistant calling on behalf of " + test_profile["name"] in identity
    assert "Never claim to be the account holder" in identity
    assert "truthfully say you are not the account holder" in identity
    assert prompt.index("AI assistant") < min(prompt.index(p) for p in compiled.stall_phrases)


def test_task_choices_wait_for_question_and_injected_instruction(compiled):
    task = compiled.sonic_prompt.split("2. Task\n", 1)[1].split("3. Authorized facts", 1)[0]
    assert "do not mention them unless the other side asks" in task
    assert SONIC_INSTRUCTION_PREFIX.strip() in task
    assert "do not volunteer them in your opening" in task
    assert "deposit_or_autopay: " + DEPOSIT_LABEL in task


def test_keypad_facts_are_not_spoken(compiled):
    facts = compiled.sonic_prompt.split("3. Authorized facts\n", 1)[1].split("4. Open-ended questions", 1)[0]
    assert "give each one only when the other side asks for it" in facts
    assert "Do not speak the account number or other keypad fields" in facts
    assert "the program sends DTMF when requested" in facts
    assert "account_number: " + compiled.allowed_fields["account_number"] in facts
