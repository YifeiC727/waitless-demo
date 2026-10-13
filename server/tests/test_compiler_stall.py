"""AC-45：Sonic 提示逐条列出 stall-phrases.yaml 的固定说法，选择类问题只许说这些（G 线；tech §6.9 第 5、7 节）。

第 3 步可见测试。说法的内容由 G 线写在 skills/decide-option/references/stall-phrases.yaml，这里不写死原文。
"""

import re
from pathlib import Path

from g_support import all_strings, profile, read_yaml, require_skills_dir, standing_rules, task_card
from waitless.models import SONIC_INSTRUCTION_PREFIX
from waitless.skills.compiler import build_sonic_prompt, compile_call
from waitless.skills.loader import load_skills


def _compiled(skills_dir: Path, test_profile: dict):
    skills = load_skills(require_skills_dir(skills_dir))
    return skills, compile_call(task_card(), skills, profile(test_profile), standing_rules(), [])


def test_ac45_every_stall_phrase_from_the_skill_is_listed(skills_dir: Path, test_profile: dict) -> None:
    _skills, compiled = _compiled(skills_dir, test_profile)
    in_file = set(all_strings(read_yaml(skills_dir / "decide-option" / "references" / "stall-phrases.yaml")))
    assert compiled.stall_phrases
    assert set(compiled.stall_phrases) <= in_file, "stall phrases come only from stall-phrases.yaml"
    for phrase in compiled.stall_phrases:
        assert phrase in compiled.sonic_prompt, f"stall phrase not listed in the prompt: {phrase!r}"


def test_ac45_prompt_limits_choices_to_stall_and_injected_text(skills_dir: Path, test_profile: dict) -> None:
    _skills, compiled = _compiled(skills_dir, test_profile)
    low = compiled.sonic_prompt.lower()
    assert re.search(r"\bchoice|\bchoose|\boption", low), "section 5 names choice questions"
    assert "confirm" in low, "section 5 names confirmation questions"
    assert SONIC_INSTRUCTION_PREFIX.strip().lower() in low, "section 7 explains the injected-instruction prefix"


def test_ac45_build_sonic_prompt_lists_the_given_phrases(skills_dir: Path, test_profile: dict) -> None:
    skills, compiled = _compiled(skills_dir, test_profile)
    phrases = ["Let me check on that for you.", "One moment while I look into it."]
    prompt = build_sonic_prompt(
        task=task_card(),
        user_name=test_profile["name"],
        allowed_fields=dict(compiled.allowed_fields),
        stall_phrases=phrases,
        skills=skills,
    )
    for phrase in phrases:
        assert phrase in prompt
