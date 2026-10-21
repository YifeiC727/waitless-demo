"""U115/U119: spoken menus remain available without live agent tools."""

import pytest

from g_support import profile, standing_rules, task_card
from waitless.skills.compiler import compile_call
from waitless.skills.loader import load_skills


MENU_RULES = (
    "- During hold music, hold or queue messages and recordings, say nothing, however long the wait.",
    '- Do not say digits or menu choices aloud. Keys are pressed for you; a "[Waitless event]" '
    'with "keys_pressed" tells you what was entered. Then keep listening.',
    "- If the phone system asks you to say your answer out loud, wait until it finishes, then say "
    "the one fitting choice once, clearly, using the wording it offered. Use the task data and company notes to choose.",
    "- If a prompt offers both speech and a key for the same choice, let Waitless handle the key and do not also say it.",
    '- If no menu choice fits and no person comes on the line, you may say "representative" once after the '
    "prompt finishes. Do not add this fallback after giving a fitting choice, and do not repeat it while waiting.",
)
OLD_RULES = (
    "After update_status returns, say nothing and call no other tool until you hear something new from the other side.",
    "While a recorded menu or a keypad prompt is playing, stay silent and listen. Do not say digits or menu choices aloud.",
)


@pytest.fixture
def compiled_prompt(skills_dir, test_profile):
    return compile_call(
        task_card(), load_skills(skills_dir), profile(test_profile), standing_rules(), [], mode="live-sim"
    ).sonic_prompt


@pytest.mark.parametrize("rule", MENU_RULES, ids=("hold", "keypad-only", "spoken-answer", "speech-and-key", "fallback"))
def test_compiled_call_contains_exact_new_rules(compiled_prompt, rule):
    assert rule in compiled_prompt.splitlines()


def test_live_prompt_has_no_tool_result_instructions(compiled_prompt):
    assert "update_status" not in compiled_prompt
    assert "Getting help" not in compiled_prompt


@pytest.mark.parametrize("rule", OLD_RULES, ids=("blanket-tool-silence", "blanket-menu-silence"))
def test_compiled_call_omits_old_blanket_silence_rules(compiled_prompt, rule):
    assert rule not in compiled_prompt


def test_menu_choice_speech_prohibition_is_only_for_keypad_prompts(compiled_prompt):
    prohibition = "Do not say digits or menu choices aloud"
    matching_lines = [line for line in compiled_prompt.splitlines() if prohibition in line]
    assert matching_lines == [MENU_RULES[1]]
    assert compiled_prompt.count(prohibition) == 1


def test_representative_fallback_appears_once(compiled_prompt):
    assert compiled_prompt.count(MENU_RULES[-1]) == 1
    assert compiled_prompt.count('"representative"') == 1
