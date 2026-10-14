"""AC-48: callers receive PolicyError with the validation cause preserved."""

import pytest
from pydantic import ValidationError

from g_support import copy_skills, edit_yaml, merged_policy_raw
from waitless.decision.policy import Policy, PolicyError
from waitless.decision.registry import load_registry
from waitless.skills.loader import load_skills


FIELDS = [
    ("interrupt_budget",),
    ("auto_answer",),
    ("auto_answer", "min_confidence"),
    ("ask_user",),
    ("ask_user", "only_if"),
    ("ask_user", "only_if", "min_stakes"),
    ("ask_user", "only_if", "max_confidence"),
    ("ask_user", "max_options"),
    ("ask_user", "timeout_sec"),
    ("ask_user", "on_timeout"),
    ("handoff_always",),
    ("never_claim_to_be_user",),
    ("learn_from_answers",),
    ("learn_from_answers", "enabled"),
    ("learn_from_answers", "max_stakes"),
]
INVALID = [
    (("interrupt_budget",), -1),
    (("interrupt_budget",), "invalid"),
    (("auto_answer", "min_confidence"), 1.1),
    (("ask_user", "only_if", "min_stakes"), 4),
    (("ask_user", "only_if", "max_confidence"), -0.1),
    (("ask_user", "max_options"), 4),
    (("ask_user", "timeout_sec"), 0),
    (("ask_user", "on_timeout"), []),
    (("handoff_always",), "ssn"),
    (("never_claim_to_be_user",), "invalid"),
    (("learn_from_answers", "enabled"), []),
    (("learn_from_answers", "max_stakes"), 2),
]


def _node(global_, path):
    for key in path:
        global_ = global_[key]
    return global_


def _assert_error(raw):
    with pytest.raises(PolicyError) as error:
        Policy.load(raw)
    assert isinstance(error.value.__cause__, ValidationError)


@pytest.mark.parametrize("path", FIELDS, ids=lambda p: ".".join(p))
def test_missing_global_fields_are_policy_errors(skills_dir, path):
    raw = merged_policy_raw(skills_dir)
    del _node(raw["global"], path[:-1])[path[-1]]
    _assert_error(raw)


@pytest.mark.parametrize("path", [(), ("auto_answer",), ("ask_user",),
                                  ("ask_user", "only_if"), ("learn_from_answers",)])
def test_unknown_global_keys_are_policy_errors(skills_dir, path):
    raw = merged_policy_raw(skills_dir)
    _node(raw["global"], path)["unknown"] = 1
    _assert_error(raw)


@pytest.mark.parametrize("path,value", INVALID)
def test_invalid_global_values_are_policy_errors(skills_dir, path, value):
    raw = merged_policy_raw(skills_dir)
    _node(raw["global"], path[:-1])[path[-1]] = value
    _assert_error(raw)


@pytest.mark.parametrize("global_", [None, [], "invalid"])
def test_invalid_global_section_is_a_policy_error(skills_dir, global_):
    raw = merged_policy_raw(skills_dir)
    raw["global"] = global_
    _assert_error(raw)


def test_registry_startup_preserves_policy_error(skills_dir, tmp_path):
    root = copy_skills(skills_dir, tmp_path)
    edit_yaml(root / "decide-option/policy.yaml",
              lambda raw: raw["global"]["ask_user"].pop("timeout_sec"))
    with pytest.raises(PolicyError) as error:
        load_registry(load_skills(root))
    assert isinstance(error.value.__cause__, ValidationError)
