"""加载 skills/，记录 commit 与内容哈希（G 线；tech §6.9；AC-39、AC-40）。

公开接口（§7）：load_skills(dir) -> dict[str, Skill]、skill_version(path)。
"""

from pathlib import Path
from typing import Any
import hashlib
import json
import re
import subprocess

import yaml
from pydantic import ValidationError

from waitless.models import (Skill, SkillVersion, IvrMap, PrecallQuestionFile,
                             TrustedSources, EvalCase, SKILL_KIND_KEY, SKILL_KINDS)

__all__ = ["NAME_PATTERN", "SkillError", "load_skills", "parse_frontmatter", "skill_version"]

NAME_PATTERN = r"^[a-z0-9-]+$"


class SkillError(Exception):
    """skill 目录或 SKILL.md 不合规（frontmatter 缺字段、name 与目录名不一致、没有结构化文件等）。"""


def load_skills(skills_dir: str | Path) -> dict[str, Skill]:
    """读取 skills_dir 下每个 skill 目录，返回 name → Skill。"""
    # 目录数由仓库内容保证，布局由测试扫描核对（tech §7.1 AC-39）。
    root = Path(skills_dir).resolve()
    if not root.is_dir():
        raise SkillError("skills directory does not exist")
    result = {}
    for path in sorted(root.iterdir()):
        if not path.is_dir() or path.name.startswith("."):
            continue
        try:
            meta, body = parse_frontmatter((path / "SKILL.md").read_text(encoding="utf-8"))
            name, description = meta.get("name"), meta.get("description")
            if (not isinstance(name, str) or name != path.name or len(name) > 64
                    or not re.fullmatch(NAME_PATTERN, name)):
                raise SkillError(f"invalid skill name: {path.name}")
            if not isinstance(description, str) or not 1 <= len(description) <= 1024:
                raise SkillError(f"invalid description: {path.name}")
            metadata = meta.get("metadata", {})
            if not isinstance(metadata, dict) or any(not isinstance(k, str) or not isinstance(v, str)
                                                      for k, v in metadata.items()):
                raise SkillError(f"invalid metadata: {path.name}")
            if SKILL_KIND_KEY in metadata and metadata[SKILL_KIND_KEY] not in SKILL_KINDS:
                raise SkillError(f"invalid skill kind: {path.name}")
            structured = [p for p in (path / "references").glob("*.yaml")]
            structured += [p for p in [path / "jev/questions.yaml", path / "policy.yaml"] if p.is_file()]
            if not structured:
                raise SkillError(f"missing structured file: {path.name}")
            for p in structured:
                data = yaml.safe_load(p.read_text(encoding="utf-8"))
                if not isinstance(data, (dict, list)) or not data:
                    raise SkillError(f"empty structured file: {p.name}")
                model = {"ivr-map.yaml": IvrMap, "pre-call-questions.yaml": PrecallQuestionFile,
                         "trusted-sources.yaml": TrustedSources}.get(p.name)
                if model:
                    model.model_validate(data)
            lines = [line for line in (path / "evals/cases.jsonl").read_text(encoding="utf-8").splitlines()
                     if line.strip()]
            if not lines:
                raise SkillError(f"missing eval cases: {path.name}")
            for line in lines:
                if EvalCase.model_validate(json.loads(line)).skill != name:
                    raise SkillError(f"eval case belongs to another skill: {path.name}")
            result[name] = Skill(name=name, description=description, metadata=metadata, body=body,
                                 path=path, files=[p.relative_to(path).as_posix() for p in _files(path)],
                                 version=skill_version(path))
        except (OSError, ValueError, yaml.YAMLError, ValidationError) as exc:
            raise SkillError(f"invalid skill {path.name}: {exc}") from exc
    return result


def parse_frontmatter(text: str) -> tuple[dict[str, Any], str]:
    """拆出 SKILL.md 的 YAML frontmatter 与正文；没有 frontmatter 抛 SkillError。"""
    lines = text.splitlines(keepends=True)
    if not lines or lines[0].strip() != "---":
        raise SkillError("SKILL.md requires YAML frontmatter")
    for index in range(1, len(lines)):
        if lines[index].strip() == "---":
            try:
                meta = yaml.safe_load("".join(lines[1:index]))
            except yaml.YAMLError as exc:
                raise SkillError("invalid frontmatter YAML") from exc
            if not isinstance(meta, dict):
                raise SkillError("frontmatter must be a mapping")
            return meta, "".join(lines[index + 1:])
    raise SkillError("unterminated frontmatter")


def skill_version(path: str | Path) -> SkillVersion:
    """commit = `git log -1 --format=%H -- <dir>`；不在 git 里为 "unknown"；content_sha256 照算（AC-40）。"""
    root = Path(path).resolve()
    digest = hashlib.sha256()
    for file in _files(root):
        digest.update(file.relative_to(root).as_posix().encode())
        digest.update(b"\0")
        digest.update(file.read_bytes())
        digest.update(b"\0")
    commit, dirty = "unknown", False
    try:
        log = subprocess.run(["git", "log", "-1", "--format=%H", "--", str(root)],
                             cwd=root, capture_output=True, text=True, check=True)
        commit = log.stdout.strip() or "unknown"
        status = subprocess.run(["git", "status", "--porcelain", "--untracked-files=all", "--", str(root)],
                                cwd=root, capture_output=True, text=True, check=True)
        dirty = bool(status.stdout.strip())
    except (OSError, subprocess.CalledProcessError):
        pass
    return SkillVersion(commit=commit, content_sha256=digest.hexdigest(), dirty=dirty)


def _files(root: Path) -> list[Path]:
    # Never traverse private test material or Git internals.
    import os
    files = []
    for base, dirs, names in os.walk(root):
        dirs[:] = [d for d in dirs if d not in {"hidden", ".git", "__pycache__"}]
        files.extend(Path(base) / n for n in names if (Path(base) / n).is_file())
    return sorted(files)
