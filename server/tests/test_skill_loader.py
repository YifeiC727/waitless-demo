"""AC-40：加载器给每个 skill 记录 commit 与 content_sha256（G 线；tech §6.9；第 3 步可见测试）。

在 tmp_path 里新建 git 仓库放 skills 副本；期望的 commit 由测试自己执行
`git log -1 --format=%H -- <dir>` 得出。不在 git 里的副本 commit 必须是 "unknown"，哈希照算。
"""

import re
import shutil
from pathlib import Path

import pytest

from g_support import SKILL_NAMES, git_repo_with_skills, isolate_git, last_commit, require_skills_dir
from waitless.skills.loader import load_skills, skill_version

HEX40 = re.compile(r"^[0-9a-f]{40}$")
HEX64 = re.compile(r"^[0-9a-f]{64}$")


def test_ac40_commit_and_hash_inside_git(skills_dir: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    isolate_git(monkeypatch, tmp_path)
    repo, root = git_repo_with_skills(skills_dir, tmp_path)
    for name in SKILL_NAMES:
        version = skill_version(root / name)
        assert HEX40.match(version.commit)
        assert version.commit == last_commit(repo, f"skills/{name}")
        assert HEX64.match(version.content_sha256)
        assert version.dirty is False


def test_ac40_outside_git_commit_is_unknown_and_hash_is_kept(
    skills_dir: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    isolate_git(monkeypatch, tmp_path)
    _repo, root = git_repo_with_skills(skills_dir, tmp_path)
    plain = tmp_path / "plain"
    shutil.copytree(require_skills_dir(skills_dir), plain)
    for name in SKILL_NAMES:
        in_git = skill_version(root / name)
        outside = skill_version(plain / name)
        assert outside.commit == "unknown"
        assert HEX64.match(outside.content_sha256)
        assert outside.content_sha256 == in_git.content_sha256, "same files must hash the same anywhere"


def test_ac40_load_skills_records_each_version(
    skills_dir: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    isolate_git(monkeypatch, tmp_path)
    repo, root = git_repo_with_skills(skills_dir, tmp_path)
    skills = load_skills(root)
    for name in SKILL_NAMES:
        assert skills[name].version.commit == last_commit(repo, f"skills/{name}")
        assert skills[name].version.content_sha256 == skill_version(root / name).content_sha256
    plain = tmp_path / "plain"
    shutil.copytree(skills_dir, plain)
    assert {s.version.commit for s in load_skills(plain).values()} == {"unknown"}


def test_ac40_uncommitted_edit_is_dirty_and_changes_hash(
    skills_dir: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    isolate_git(monkeypatch, tmp_path)
    _repo, root = git_repo_with_skills(skills_dir, tmp_path)
    before = skill_version(root / "navigate-ivr")
    skill_md = root / "navigate-ivr" / "SKILL.md"
    skill_md.write_text(skill_md.read_text(encoding="utf-8") + "\nOne more line.\n", encoding="utf-8")
    after = skill_version(root / "navigate-ivr")
    assert after.dirty is True
    assert after.commit == before.commit
    assert after.content_sha256 != before.content_sha256
