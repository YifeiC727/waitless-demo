"""加载 skills/，记录 commit 与内容哈希（G 线；tech §6.9；AC-39、AC-40）。

公开接口（§7）：load_skills(dir) -> dict[str, Skill]、skill_version(path)。
"""

from pathlib import Path
from typing import Any

from waitless.models import Skill, SkillVersion

__all__ = ["NAME_PATTERN", "SkillError", "load_skills", "parse_frontmatter", "skill_version"]

NAME_PATTERN = r"^[a-z0-9-]+$"


class SkillError(Exception):
    """skill 目录或 SKILL.md 不合规（frontmatter 缺字段、name 与目录名不一致、没有结构化文件等）。"""


def load_skills(skills_dir: str | Path) -> dict[str, Skill]:
    """读取 skills_dir 下每个 skill 目录，返回 name → Skill。"""
    # todo(G): 恰好 6 个目录：assisted-call、find-official-number、navigate-ivr、decide-option、handoff-to-user、
    #          company-lakeshore-energy；没有 hold-and-callback（AC-39）。
    # todo(G): 每个目录：SKILL.md（name 与目录名一致、≤64、^[a-z0-9-]+$；description 1–1024）；
    #          至少一个结构化文件（jev/questions.yaml、policy.yaml 或 references/*.yaml）；evals/cases.jsonl ≥1 行。
    # todo(G): metadata 放 waitless-kind、company-id（键名用 models.SKILL_KIND_KEY、SKILL_COMPANY_ID_KEY，
    #          取值 models.SKILL_KINDS；缺口 A-G-4）；先对照 Agent Skills 规范核对是否允许（§6.9）。
    # todo(G): 公司 skill 的 references/ivr-map.yaml、pre-call-questions.yaml 与 find-official-number 的
    #          trusted-sources.yaml 加载时按 models.IvrMap、PrecallQuestionFile、TrustedSources 校验，不合规抛 SkillError（A-G-4）。
    raise NotImplementedError


def parse_frontmatter(text: str) -> tuple[dict[str, Any], str]:
    """拆出 SKILL.md 的 YAML frontmatter 与正文；没有 frontmatter 抛 SkillError。"""
    raise NotImplementedError


def skill_version(path: str | Path) -> SkillVersion:
    """commit = `git log -1 --format=%H -- <dir>`；不在 git 里为 "unknown"；content_sha256 照算（AC-40）。"""
    # todo(G): content_sha256 对目录内文件按相对路径排序后逐个（路径 + 内容）哈希；dirty 表示工作区有未提交改动。
    # todo(G): 子进程只读执行 git，不改仓库；不在 git 里或没有 git 可执行文件时不报错。
    raise NotImplementedError
