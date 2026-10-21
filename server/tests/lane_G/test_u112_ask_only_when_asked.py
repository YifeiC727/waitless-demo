"""U112: live automation defers personal decisions to the account holder."""

from g_support import profile, standing_rules, task_card
from waitless.skills.compiler import compile_call
from waitless.skills.loader import load_skills


ASK_ONLY_WHEN_ASKED = (
    "If it asks for something you were not given (such as a date of birth or card number) "
    "or for a decision that belongs to the account holder (such as a fee), say the account "
    "holder will take care of that with a representative, and ask for a representative."
)


def test_compiled_call_prompt_asks_only_about_decisions_actually_raised(
    skills_dir, test_profile,
):
    compiled = compile_call(
        task_card(), load_skills(skills_dir), profile(test_profile), standing_rules(), [], mode="live-sim"
    )

    assert ASK_ONLY_WHEN_ASKED in compiled.sonic_prompt
