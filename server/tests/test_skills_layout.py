"""AC-39：skills/ 恰有 7 个合规的 skill 目录，加载器逐个解析（G 线；tech §6.9、§7.1；第 3 步可见测试）。

先直接扫描仓库的 skills/（不经被测代码），再经 load_skills() 加载；不合规的副本放在 tmp_path 里。
"""

import json
import re
from pathlib import Path

import pytest
import yaml

from g_support import (
    COMPANY_SKILL,
    SKILL_NAMES,
    STRUCTURED_FILES,
    copy_skills,
    edit_yaml,
    frontmatter,
    read_yaml,
    require_skills_dir,
    write_frontmatter,
)
from waitless.models import EvalCase, IvrMap, PrecallQuestionFile, TrustedSources
from waitless.skills.loader import SkillError, load_skills, parse_frontmatter

NAME_RE = re.compile(r"^[a-z0-9-]+$")


# ---------------------------------------------------------------------------
# 仓库内容
# ---------------------------------------------------------------------------


def test_ac39_exactly_seven_skill_directories(skills_dir: Path) -> None:
    require_skills_dir(skills_dir)
    dirs = sorted(p.name for p in skills_dir.iterdir() if p.is_dir() and not p.name.startswith("."))
    assert dirs == sorted(SKILL_NAMES)
    assert not (skills_dir / "hold-and-callback").exists()


@pytest.mark.parametrize("name", SKILL_NAMES)
def test_ac39_skill_md_frontmatter(skills_dir: Path, name: str) -> None:
    meta, _body = frontmatter(skills_dir / name / "SKILL.md")
    assert meta.get("name") == name
    assert len(meta["name"]) <= 64 and NAME_RE.match(meta["name"])
    desc = meta.get("description")
    assert isinstance(desc, str) and 1 <= len(desc) <= 1024


@pytest.mark.parametrize("name", SKILL_NAMES)
def test_ac39_structured_files_listed_in_tech_6_9(skills_dir: Path, name: str) -> None:
    for rel in STRUCTURED_FILES[name]:
        path = skills_dir / name / rel
        assert path.is_file(), f"{name}: missing {rel}"
        doc = yaml.safe_load(path.read_text(encoding="utf-8"))
        assert isinstance(doc, (dict, list)) and doc, f"{name}/{rel} is empty or not a YAML mapping/list"


@pytest.mark.parametrize("name", SKILL_NAMES)
def test_ac39_eval_cases_have_at_least_one_valid_line(skills_dir: Path, name: str) -> None:
    path = skills_dir / name / "evals" / "cases.jsonl"
    assert path.is_file(), f"{name}: missing evals/cases.jsonl"
    lines = [ln for ln in path.read_text(encoding="utf-8").splitlines() if ln.strip()]
    assert lines, f"{name}: evals/cases.jsonl has no case"
    for ln in lines:
        case = EvalCase.model_validate(json.loads(ln))
        assert case.skill == name


# ---------------------------------------------------------------------------
# 加载器
# ---------------------------------------------------------------------------


def test_ac39_load_skills_returns_the_seven_skills(skills_dir: Path) -> None:
    require_skills_dir(skills_dir)
    skills = load_skills(skills_dir)
    assert sorted(skills) == sorted(SKILL_NAMES)
    for name, skill in skills.items():
        meta, body = frontmatter(skills_dir / name / "SKILL.md")
        assert skill.name == name
        assert skill.description == meta["description"]
        assert skill.body.strip() == body.strip()
        assert not skill.body.lstrip().startswith("---")
        assert Path(skill.path).resolve() == (skills_dir / name).resolve()
        assert "SKILL.md" in skill.files
        assert "evals/cases.jsonl" in skill.files
        for rel in STRUCTURED_FILES[name]:
            assert rel in skill.files, f"{name}: {rel} not in Skill.files"


def test_ac39_parse_frontmatter_splits_yaml_and_body() -> None:
    meta, body = parse_frontmatter(
        "---\nname: navigate-ivr\ndescription: Walk automated phone menus.\n---\n# Navigate IVR\n\nBody text.\n"
    )
    assert meta == {"name": "navigate-ivr", "description": "Walk automated phone menus."}
    assert body.strip() == "# Navigate IVR\n\nBody text."


def test_ac39_parse_frontmatter_requires_frontmatter() -> None:
    with pytest.raises(SkillError):
        parse_frontmatter("# Navigate IVR\n\nNo frontmatter here.\n")


BAD_FRONTMATTER = {
    "name_mismatch": lambda meta: meta.update(name="navigate-ivr-v2"),
    "name_missing": lambda meta: meta.pop("name"),
    "description_missing": lambda meta: meta.pop("description"),
    "description_empty": lambda meta: meta.update(description=""),
    "description_over_1024": lambda meta: meta.update(description="x" * 1025),
}


@pytest.mark.parametrize("case", sorted(BAD_FRONTMATTER))
def test_ac39_loader_rejects_bad_frontmatter(skills_dir: Path, tmp_path: Path, case: str) -> None:
    root = copy_skills(skills_dir, tmp_path)
    path = root / "navigate-ivr" / "SKILL.md"
    meta, body = frontmatter(path)
    BAD_FRONTMATTER[case](meta)
    write_frontmatter(path, meta, body)
    with pytest.raises(SkillError):
        load_skills(root)


@pytest.mark.parametrize("bad_name", ["navigate-ivr-" + "x" * 52, "Navigate-IVR", "navigate_ivr"])
def test_ac39_loader_rejects_bad_names(skills_dir: Path, tmp_path: Path, bad_name: str) -> None:
    """目录名与 name 一起改：65 个字符、含大写、含下划线都不合规。"""
    root = copy_skills(skills_dir, tmp_path)
    (root / "navigate-ivr").rename(root / bad_name)
    path = root / bad_name / "SKILL.md"
    meta, body = frontmatter(path)
    meta["name"] = bad_name
    write_frontmatter(path, meta, body)
    with pytest.raises(SkillError):
        load_skills(root)


def test_ac39_loader_requires_a_structured_file(skills_dir: Path, tmp_path: Path) -> None:
    root = copy_skills(skills_dir, tmp_path)
    skill = root / "navigate-ivr"
    for path in [skill / "jev" / "questions.yaml", skill / "policy.yaml", *(skill / "references").glob("*.yaml")]:
        if path.exists():
            path.unlink()
    with pytest.raises(SkillError):
        load_skills(root)


@pytest.mark.parametrize("how", ["missing", "empty"])
def test_ac39_loader_requires_eval_cases(skills_dir: Path, tmp_path: Path, how: str) -> None:
    root = copy_skills(skills_dir, tmp_path)
    cases = root / "handoff-to-user" / "evals" / "cases.jsonl"
    if how == "missing":
        cases.unlink()
    else:
        cases.write_text("", encoding="utf-8")
    with pytest.raises(SkillError):
        load_skills(root)


def test_ac39_reference_files_match_the_frozen_models(skills_dir: Path) -> None:
    """缺口 A-G-4（定稿 frozen_models）：三份 references 文件分别过 IvrMap、PrecallQuestionFile、TrustedSources。"""
    refs = require_skills_dir(skills_dir) / COMPANY_SKILL / "references"
    IvrMap.model_validate(read_yaml(refs / "ivr-map.yaml"))
    PrecallQuestionFile.model_validate(read_yaml(refs / "pre-call-questions.yaml"))
    TrustedSources.model_validate(read_yaml(skills_dir / "find-official-number" / "references" / "trusted-sources.yaml"))


def test_ac39_loader_rejects_a_reference_file_that_breaks_its_model(skills_dir: Path, tmp_path: Path) -> None:
    root = copy_skills(skills_dir, tmp_path)
    edit_yaml(root / COMPANY_SKILL / "references" / "ivr-map.yaml", lambda doc: doc.__setitem__("x_unknown_key", 1))
    with pytest.raises(SkillError):
        load_skills(root)
