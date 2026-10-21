"""Malformed policy/registry structures must fail with the public errors."""

import pytest

from g_support import copy_skills, merged_policy_raw, read_yaml, write_yaml
from waitless.decision.policy import Policy, PolicyError
from waitless.decision.registry import RegistryError, load_registry
from waitless.skills.loader import load_skills


@pytest.mark.parametrize("path,value", [
    (("call_round", "order"), None),
    (("call_round", "order"), 7),
    (("call_round", "actions"), ["release"]),
    (("call_round", "actions"), "release"),
    (("said_as_decided", "safe_default"), []),
    (("said_as_decided", "safe_default"), {}),
])
def test_policy_rejects_malformed_execution_config(skills_dir, path, value):
    raw = merged_policy_raw(skills_dir)
    raw["decision_points"][path[0]][path[1]] = value
    with pytest.raises(PolicyError):
        Policy.load(raw)


@pytest.mark.parametrize("relative", ["jev/questions.yaml", "policy.yaml"])
@pytest.mark.parametrize("shape", ["document_list", "points_list", "points_null"])
def test_registry_rejects_malformed_document_shapes(skills_dir, tmp_path, relative, shape):
    root = copy_skills(skills_dir, tmp_path)
    skills = load_skills(root)
    path = root / "decide-option" / relative
    raw = read_yaml(path)
    if shape == "document_list":
        raw = [raw]
    else:
        raw["decision_points"] = ["call_round"] if shape == "points_list" else None
    write_yaml(path, raw)
    with pytest.raises(RegistryError):
        load_registry(skills)


@pytest.mark.parametrize("value", [None, [], 3, "invalid"])
def test_registry_rejects_nonmapping_point_policy(skills_dir, tmp_path, value):
    root = copy_skills(skills_dir, tmp_path)
    skills = load_skills(root)
    path = root / "decide-option/policy.yaml"
    raw = read_yaml(path)
    raw["decision_points"]["call_round"] = value
    write_yaml(path, raw)
    with pytest.raises(RegistryError):
        load_registry(skills)
