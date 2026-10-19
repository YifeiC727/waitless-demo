"""U115: answer spoken menus without treating tool results as a reason to speak."""

import pytest

from g_support import profile, standing_rules, task_card
from waitless.skills.compiler import compile_call
from waitless.skills.loader import load_skills


UPDATE_STATUS_RULE = (
    "- After update_status returns, do not speak or call another tool just to fill "
    "the silence. If the other side has already asked for an answer you still owe, "
    "answer it once, or follow Getting help if the answer requires it; otherwise "
    "listen until they speak again. A tool result itself never calls for a spoken reply."
)
MENU_RULES = (
    "- While any recorded prompt is playing, stay silent and listen until it finishes.",
    "- For a keypad-only prompt: Do not say digits or menu choices aloud. Keys are "
    "pressed for you. After the phone system speaks, Waitless asks Jev whether a "
    "key or one of your authorized numbers should be entered, and presses it once. "
    'A "[Waitless event]" with "keys_pressed" tells you what was entered. Then keep '
    "listening for what comes next.",
    "- If the phone system explicitly asks for a spoken answer, wait until it "
    "finishes, then say the one fitting choice once, clearly, using the wording "
    "it offered. Use the task data and company notes when they already settle the "
    "choice. Do not call a tool instead of giving an answer you already know. If "
    "it is an unsettled choice that belongs to the account holder, follow Getting help.",
    "- If the prompt offers both speech and a key for the same choice, let "
    "Waitless handle the key and do not also say that choice aloud.",
    "- If no menu choice fits and no person comes on the line, you may say "
    '"representative" once as a fallback, after the prompt finishes. Do not add '
    "this fallback after giving a fitting choice, and do not repeat it while waiting.",
)
OLD_RULES = (
    "After update_status returns, say nothing and call no other tool until you "
    "hear something new from the other side.",
    "While a recorded menu or a keypad prompt is playing, stay silent and listen. "
    "Do not say digits or menu choices aloud.",
)


@pytest.fixture
def compiled_prompt(skills_dir, test_profile):
    return compile_call(
        task_card(), load_skills(skills_dir), profile(test_profile), standing_rules(), []
    ).sonic_prompt


@pytest.mark.parametrize(
    "rule", (UPDATE_STATUS_RULE, *MENU_RULES),
    ids=("update-status", "listen", "keypad-only", "spoken-answer", "speech-and-key", "fallback"),
)
def test_compiled_call_contains_exact_new_rules(compiled_prompt, rule):
    assert rule in compiled_prompt.splitlines()


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
