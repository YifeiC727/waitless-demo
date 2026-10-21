"""AC-41：compile_call 产出 sonic_prompt、jev_templates、policy、ivr_map 等（G 线；tech §6.9；第 3 步可见测试）。"""

import json
from pathlib import Path

import pytest

from g_support import COMPANY_SKILL, find_key, ivr_rules, profile, read_yaml, require_skills_dir, standing_rules, task_card
from waitless.models import CALL_ROUND_QUESTIONS, ROUND_INTENTS, SAY_TEMPLATE_KEYS, CompiledCall, IvrMap
from waitless.skills.compiler import compile_call
from waitless.skills.loader import SkillError, load_skills


def _compile(skills_dir: Path, test_profile: dict, **card_kw) -> tuple[dict, CompiledCall]:
    skills = load_skills(require_skills_dir(skills_dir))
    return skills, compile_call(task_card(**card_kw), skills, profile(test_profile), standing_rules(), [])


def test_ac41_returns_the_four_artifacts(skills_dir: Path, test_profile: dict) -> None:
    _skills, compiled = _compile(skills_dir, test_profile)
    assert isinstance(compiled, CompiledCall)
    assert compiled.sonic_prompt.strip()
    assert compiled.jev_templates
    dumped = json.dumps(compiled.jev_templates)
    for qid in CALL_ROUND_QUESTIONS:
        assert qid in dumped, f"jev_templates lacks the call_round question {qid}"
    decide_option_policy = read_yaml(skills_dir / "decide-option" / "policy.yaml")
    assert compiled.policy.get("global") == decide_option_policy["global"]
    ivr_map = IvrMap.model_validate(read_yaml(skills_dir / COMPANY_SKILL / "references" / "ivr-map.yaml"))
    assert compiled.ivr_map == ivr_map.model_dump()  # 缺口 A-G-4


def test_ac41_carries_call_data_and_versions(skills_dir: Path, test_profile: dict) -> None:
    skills, compiled = _compile(skills_dir, test_profile)
    assert compiled.task_card == task_card()
    assert set(SAY_TEMPLATE_KEYS) <= set(compiled.say_templates)
    for key in SAY_TEMPLATE_KEYS:
        assert isinstance(compiled.say_templates[key], str) and compiled.say_templates[key].strip()
    assert compiled.lexicons and compiled.stall_phrases
    intents = json.dumps(compiled.intents)
    for intent in ROUND_INTENTS:
        assert intent in intents, f"compiled intents lack {intent}"
    names = {ref.name for ref in compiled.skill_versions}
    assert {"decide-option", "handoff-to-user", COMPANY_SKILL} <= names
    for ref in compiled.skill_versions:
        assert ref.commit == skills[ref.name].version.commit
        assert ref.content_sha256 == skills[ref.name].version.content_sha256


def test_ac41_unknown_company_is_rejected(skills_dir: Path, test_profile: dict) -> None:
    skills = load_skills(require_skills_dir(skills_dir))
    with pytest.raises((SkillError, ValueError, LookupError)):
        compile_call(task_card(company_id="acme-utility"), skills, profile(test_profile), standing_rules(), [])


def test_ac41_menu_parsing_inputs_come_from_ivr_rules(skills_dir: Path, test_profile: dict) -> None:
    """缺口 E-G-4、G-G-03：press_patterns 与 digit_words 由 compile_call 从 navigate-ivr 的 ivr-rules.yaml 带出。"""
    _skills, compiled = _compile(skills_dir, test_profile)
    rules = ivr_rules(skills_dir)
    patterns = find_key(rules, "press_patterns")
    words = find_key(rules, "digit_words")
    assert patterns and words, "ivr-rules.yaml needs press_patterns and digit_words"
    assert compiled.press_patterns == list(patterns[0]) and compiled.press_patterns
    assert compiled.digit_words == dict(words[0]) and compiled.digit_words.get("nine") == "9"
