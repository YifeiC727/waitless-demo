"""Only name and description are mandatory frontmatter fields (§6.9)."""

import pytest

from g_support import copy_skills, frontmatter, write_frontmatter
from waitless.skills.loader import SkillError, load_skills


@pytest.mark.parametrize("metadata", [None, {}, {"owner": "Waitless"}])
def test_loader_accepts_optional_metadata(skills_dir, tmp_path, metadata):
    root = copy_skills(skills_dir, tmp_path)
    path = root / "navigate-ivr/SKILL.md"
    meta, body = frontmatter(path)
    if metadata is None:
        meta.pop("metadata")
    else:
        meta["metadata"] = metadata
    write_frontmatter(path, meta, body)
    skills = load_skills(root)
    assert skills["navigate-ivr"].metadata == (metadata or {})


@pytest.mark.parametrize("kind", ["unknown", 123])
def test_loader_still_rejects_invalid_declared_kind(skills_dir, tmp_path, kind):
    root = copy_skills(skills_dir, tmp_path)
    path = root / "navigate-ivr/SKILL.md"
    meta, body = frontmatter(path)
    meta["metadata"]["waitless-kind"] = kind
    write_frontmatter(path, meta, body)
    with pytest.raises(SkillError):
        load_skills(root)
