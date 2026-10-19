"""U112: live call instructions reserve interruptions for actual decisions."""

from g_support import profile, standing_rules, task_card
from waitless.skills.compiler import compile_call
from waitless.skills.loader import load_skills


ASK_ONLY_WHEN_ASKED = (
    "Ask only about a decision the other side has actually put to you on this call; "
    "never ask ahead about a topic that might come up later, because interruptions "
    "of the account holder are limited and a representative may still need them."
)


def test_compiled_call_prompt_asks_only_about_decisions_actually_raised(
    skills_dir, test_profile,
):
    compiled = compile_call(
        task_card(), load_skills(skills_dir), profile(test_profile), standing_rules(), []
    )

    assert ASK_ONLY_WHEN_ASKED in compiled.sonic_prompt
