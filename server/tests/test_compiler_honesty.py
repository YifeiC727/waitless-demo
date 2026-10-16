"""AC-43：phone-agent 提示写明身份与诚实条款（G 线；tech E131；第 3 步可见测试）。"""

import re
from pathlib import Path

from g_support import profile, require_skills_dir, standing_rules, task_card
from waitless.skills.compiler import compile_call
from waitless.skills.loader import load_skills

NEGATION = re.compile(r"\b(not|never|no)\b|n't", re.I)


def _prompt(skills_dir: Path, test_profile: dict) -> tuple[str, list[str]]:
    skills = load_skills(require_skills_dir(skills_dir))
    compiled = compile_call(task_card(), skills, profile(test_profile), standing_rules(), [])
    return compiled.sonic_prompt, compiled.stall_phrases


def test_ac43_opening_says_it_is_an_ai_assistant_for_the_user(skills_dir: Path, test_profile: dict) -> None:
    prompt, _ = _prompt(skills_dir, test_profile)
    assert "ai assistant" in prompt.lower()
    assert test_profile["name"] in prompt


def test_ac43_never_claims_to_be_the_account_holder(skills_dir: Path, test_profile: dict) -> None:
    prompt, _ = _prompt(skills_dir, test_profile)
    sentences = [s for s in re.split(r"(?<=[.!?])\s+|\n+", prompt) if "account holder" in s.lower()]
    assert sentences, "the prompt must say how to answer 'are you the account holder?'"
    assert any(NEGATION.search(s) for s in sentences), sentences
