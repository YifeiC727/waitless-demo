"""Reject incomplete policy at startup rather than failing during a call."""

import pytest

from g_support import copy_skills, edit_yaml, merged_policy_raw
from waitless.decision.policy import Policy, PolicyError
from waitless.decision.registry import load_registry
from waitless.skills.loader import load_skills


@pytest.mark.parametrize("step", ["menu", "conversation"])
def test_policy_requires_each_round_confidence_threshold(skills_dir, step):
    raw = merged_policy_raw(skills_dir)
    del raw["decision_points"]["call_round"][step]["min_confidence"]
    with pytest.raises(PolicyError):
        Policy.load(raw)


def test_registry_validates_policy_before_first_call(skills_dir, tmp_path):
    root = copy_skills(skills_dir, tmp_path)
    edit_yaml(root / "decide-option/policy.yaml",
              lambda raw: raw["decision_points"]["call_round"].__setitem__("order", ["confidence"]))
    with pytest.raises(PolicyError):
        load_registry(load_skills(root))
