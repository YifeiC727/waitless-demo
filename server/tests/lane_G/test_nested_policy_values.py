"""Nested policy validation must leave executable, typed values."""

import pytest

from g_support import merged_policy_raw
from waitless.decision.policy import Policy


@pytest.mark.parametrize("enabled,expected", [("false", False), ("true", True)])
def test_learning_uses_validated_boolean(skills_dir, enabled, expected):
    raw = merged_policy_raw(skills_dir)
    raw["global"]["learn_from_answers"]["enabled"] = enabled
    policy = Policy.load(raw)
    assert policy.should_learn(1) is expected
    assert policy.global_.learn_from_answers["enabled"] is expected


def test_nested_numeric_values_are_executable_after_validation(skills_dir):
    raw = merged_policy_raw(skills_dir)
    raw["global"]["learn_from_answers"]["max_stakes"] = "1"
    raw["global"]["auto_answer"]["min_confidence"] = "0.85"
    raw["global"]["ask_user"]["timeout_sec"] = "20"
    raw["global"]["ask_user"]["max_options"] = "3"
    raw["global"]["ask_user"]["only_if"]["min_stakes"] = "2"
    policy = Policy.load(raw)
    assert policy.should_learn(1) is True
    assert policy.should_learn(2) is False
    global_ = policy.global_
    assert global_.auto_answer["min_confidence"] == 0.85
    assert global_.ask_user["timeout_sec"] == 20.0
    assert global_.ask_user["max_options"] == 3
    assert global_.ask_user["only_if"]["min_stakes"] == 2
