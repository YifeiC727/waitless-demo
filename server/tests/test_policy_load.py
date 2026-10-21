"""AC-48：decide-option/policy.yaml 的 global 段取原方案的值，缺字段加载即报错（G 线；tech §6.6、§7.1）。

第 3 步可见测试。先读文件本身核对 7 个值，再经 Policy.load() 核对；缺字段的副本只在内存里改。
"""

import copy
from pathlib import Path

import pytest
from pydantic import ValidationError

from g_support import loaded_policy, merged_policy_raw, policy_raw, read_yaml, require_skills_dir
from waitless.decision.policy import Policy, PolicyError
from waitless.models import FORBIDDEN_FIELD_KEYS


def test_ac48_global_values_in_the_file(skills_dir: Path) -> None:
    g = read_yaml(require_skills_dir(skills_dir) / "decide-option" / "policy.yaml")["global"]
    assert g["interrupt_budget"] == 1
    assert g["repeat_hold_ms"] == 20000
    assert g["auto_answer"]["min_confidence"] == 0.85
    assert g["ask_user"]["max_options"] == 3
    assert g["ask_user"]["timeout_sec"] == 20
    assert sorted(g["handoff_always"]) == sorted(FORBIDDEN_FIELD_KEYS)
    assert g["never_claim_to_be_user"] is True
    assert g["learn_from_answers"]["enabled"] is True


def test_ac48_policy_load_exposes_the_global_values(skills_dir: Path) -> None:
    policy = loaded_policy(require_skills_dir(skills_dir))
    g = policy.global_
    assert g.interrupt_budget == 1
    assert g.repeat_hold_ms == 20000
    assert g.auto_answer["min_confidence"] == 0.85
    assert g.ask_user["max_options"] == 3
    assert g.ask_user["timeout_sec"] == 20
    assert sorted(g.handoff_always) == sorted(FORBIDDEN_FIELD_KEYS)
    assert g.never_claim_to_be_user is True
    assert g.learn_from_answers["enabled"] is True


MISSING = [
    ("interrupt_budget",),
    ("repeat_hold_ms",),
    ("auto_answer", "min_confidence"),
    ("ask_user", "max_options"),
    ("ask_user", "timeout_sec"),
    ("handoff_always",),
    ("never_claim_to_be_user",),
    ("learn_from_answers", "enabled"),
]


@pytest.mark.parametrize("path", MISSING, ids=[".".join(p) for p in MISSING])
def test_ac48_missing_global_field_fails_to_load(skills_dir: Path, path: tuple[str, ...]) -> None:
    raw = copy.deepcopy(policy_raw(require_skills_dir(skills_dir)))
    node = raw["global"]
    for key in path[:-1]:
        node = node[key]
    del node[path[-1]]
    with pytest.raises((PolicyError, ValidationError)):
        Policy.load(raw)


def test_ac48_registry_policy_raw_is_the_merged_policy_files(skills_dir: Path) -> None:
    """缺口 G-G-02（定稿 registry_policy_raw）：policy_raw() =
    {version, decide-option 的 global, 各 skill decision_points 的并集}。"""
    raw = policy_raw(require_skills_dir(skills_dir))
    assert raw == merged_policy_raw(skills_dir)
    assert set(raw) == {"version", "global", "decision_points"}
