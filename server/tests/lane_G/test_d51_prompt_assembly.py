"""AC-109: exact skill text plus authorized task data; text-sim data retained."""

import json

from g_support import (ACCOUNT_NUMBER, COMPANY_SKILL, copy_skills, decoy_profile,
                       frontmatter, ivr_rules, merged_policy_raw, profile,
                       read_yaml, standing_rules, task_card)
from waitless.models import FORBIDDEN_FIELD_KEYS, SONIC_INSTRUCTION_PREFIX
from waitless.skills.compiler import compile_call
from waitless.skills.loader import load_skills


def _compile(root, test_profile, decoys=None, task=None):
    skills = load_skills(root)
    prof = decoy_profile(test_profile, decoys) if decoys else profile(test_profile)
    return compile_call(task or task_card(), skills, prof, standing_rules(), [])


def test_prompt_is_skill_text_plus_data(skills_dir, test_profile):
    compiled = _compile(skills_dir, test_profile)
    path = skills_dir / "phone-agent/SKILL.md"
    assert path.is_file()
    _, body = frontmatter(path)
    notes = (skills_dir / COMPANY_SKILL / "references/agent-notes.md").read_text(encoding="utf-8")
    data = {
        "goal": task_card().goal,
        "effective_date": "2026-11-01",
        "precall_choices": [{"topic": "Pay a deposit or enroll in autopay?", "choice": "Pay a security deposit"}],
        "standing_rules": [rule.rule_text for rule in standing_rules()],
        "authorized_facts": {**test_profile, "account_number": ACCOUNT_NUMBER},
    }
    assert not set(data["authorized_facts"]) & set(FORBIDDEN_FIELD_KEYS)
    assert compiled.sonic_prompt == body + "\n\n" + notes + "\n\n# Task data\n" + json.dumps(
        data, ensure_ascii=False, indent=2, sort_keys=True)


def test_prompt_has_no_templates_or_stall(skills_dir, test_profile, decoy_sensitive):
    compiled = _compile(skills_dir, test_profile, decoy_sensitive)
    phrases = read_yaml(skills_dir / "decide-option/references/stall-phrases.yaml")["phrases"]
    for text in [*phrases, *compiled.say_templates.values(), SONIC_INSTRUCTION_PREFIX, *decoy_sensitive.values()]:
        assert text not in compiled.sonic_prompt


def test_prompt_follows_skill_edits(skills_dir, test_profile, tmp_path):
    root = copy_skills(skills_dir, tmp_path)
    path = root / "phone-agent/SKILL.md"
    assert path.is_file()
    path.write_text(path.read_text(encoding="utf-8") + "\nZebra marker sentence.\n", encoding="utf-8")
    assert "Zebra marker sentence." in _compile(root, test_profile).sonic_prompt


def test_tool_descriptions_come_from_skill(skills_dir, test_profile):
    compiled = _compile(skills_dir, test_profile)
    path = skills_dir / "phone-agent/references/tools.yaml"
    assert path.is_file()
    assert compiled.agent_tool_descriptions == read_yaml(path)


def test_textsim_fields_unchanged(skills_dir, test_profile):
    compiled = _compile(skills_dir, test_profile)
    option = skills_dir / "decide-option"
    assert compiled.stall_phrases == read_yaml(option / "references/stall-phrases.yaml")["phrases"]
    assert compiled.say_templates == {
        **read_yaml(option / "references/say-templates.yaml")["say_templates"],
        **read_yaml(skills_dir / "handoff-to-user/references/handoff.yaml")["say_templates"],
    }
    policy = merged_policy_raw(skills_dir)
    policy["precall_questions"] = read_yaml(skills_dir / COMPANY_SKILL / "references/pre-call-questions.yaml")["questions"]
    policy["ivr_rules"] = ivr_rules(skills_dir)
    assert compiled.policy == policy
    assert compiled.press_patterns == ivr_rules(skills_dir)["press_patterns"]


def test_prompt_allows_missing_company_notes(skills_dir, test_profile, tmp_path):
    root = copy_skills(skills_dir, tmp_path)
    notes = root / COMPANY_SKILL / "references/agent-notes.md"
    if notes.exists():
        notes.unlink()
    compiled = _compile(root, test_profile)
    path = root / "phone-agent/SKILL.md"
    assert path.is_file()
    _, body = frontmatter(path)
    assert compiled.sonic_prompt.startswith(body + "\n\n\n\n# Task data\n")


def test_prompt_omits_unselected_choices_and_preserves_task_text(skills_dir, test_profile):
    task = task_card(selected=None, goal="Start service — keep {literal} text.").model_copy(update={"effective_date": None})
    compiled = _compile(skills_dir, test_profile, task=task)
    assert "\n\n# Task data\n" in compiled.sonic_prompt
    data = json.loads(compiled.sonic_prompt.split("\n\n# Task data\n", 1)[1])
    assert data["goal"] == task.goal
    assert "—" in compiled.sonic_prompt
    assert data["effective_date"] is None
    assert data["precall_choices"] == []
