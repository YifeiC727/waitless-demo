"""AC-108: the live phone agent instructions and tools are skill assets."""

from e_evals.test_eval_runner import _case_lines
from g_support import COMPANY_SKILL, frontmatter, read_yaml

TOOLS = {"press_keys", "update_status", "ask_user", "ask_jev", "request_handoff", "end_call"}


def test_tools_yaml_declares_exactly_six_tools(skills_dir):
    path = skills_dir / "phone-agent/references/tools.yaml"
    assert path.is_file()
    tools = read_yaml(path)
    assert set(tools) == TOOLS
    assert all(isinstance(value, str) and value.strip() for value in tools.values())


def test_phone_agent_frontmatter(skills_dir):
    path = skills_dir / "phone-agent/SKILL.md"
    assert path.is_file()
    meta, body = frontmatter(path)
    assert meta["name"] == "phone-agent"
    assert isinstance(meta["description"], str) and meta["description"].strip()
    assert meta["metadata"]["waitless-kind"] == "harness"
    assert all(tool in body for tool in TOOLS)
    assert "[Waitless event]" in body


def test_jev_yaml_has_instructions(skills_dir):
    path = skills_dir / "phone-agent/references/jev.yaml"
    assert path.is_file()
    doc = read_yaml(path)
    assert set(doc) == {"ask_jev_instructions"}
    assert isinstance(doc["ask_jev_instructions"], str) and doc["ask_jev_instructions"].strip()


def test_agent_notes_are_prose_not_rules(skills_dir):
    path = skills_dir / COMPANY_SKILL / "references/agent-notes.md"
    assert path.is_file()
    notes = path.read_text(encoding="utf-8")
    assert notes.strip()
    for token in (r"\d", "^", "$", "menu_path", "press_patterns", "{"):
        assert token not in notes


def test_phone_agent_has_eval_case(skills_dir):
    path = skills_dir / "phone-agent/evals/cases.jsonl"
    assert path.is_file()
    cases = [case for skill, case in _case_lines(skills_dir) if skill == "phone-agent"]
    assert cases
    assert all(case.skill == "phone-agent" for case in cases)
