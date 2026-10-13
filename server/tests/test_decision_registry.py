"""AC-46：决策点注册表完整，只有 choice/noul/score 题型（G 线；tech §6.6、§7.1；第 3 步可见测试）。

另含歧义定稿 A-06（infer_from_questions）：题名全局唯一，registry.point_of(question_ids) 反推决策点
（point_of 待骨架 r2 加入）。
"""

from pathlib import Path
from typing import Any

import pytest

from g_support import copy_skills, edit_yaml, points_index, policy_index, read_yaml, require_skills_dir
from waitless.decision.policy import PolicyError
from waitless.decision.registry import RegistryError, load_registry
from waitless.models import CALL_ROUND_QUESTIONS, DECISION_POINT_CODES
from waitless.skills.loader import load_skills

QUESTION_TYPES = {"choice", "noul", "score"}


def _thresholds(doc: Any) -> list[float]:
    """policy 条目里所有 min_confidence / min_probability 的值（递归）。"""
    found: list[float] = []
    if isinstance(doc, dict):
        for k, v in doc.items():
            if k in ("min_confidence", "min_probability") and isinstance(v, (int, float)):
                found.append(float(v))
            found.extend(_thresholds(v))
    elif isinstance(doc, list):
        for v in doc:
            found.extend(_thresholds(v))
    return found


def _registry(root: Path):
    return load_registry(load_skills(root)).validate()


def test_ac46_registry_covers_all_eight_points(skills_dir: Path) -> None:
    registry = _registry(require_skills_dir(skills_dir))
    for point in DECISION_POINT_CODES:
        spec = registry.point(point)
        assert spec.questions, f"{point} has no questions"
        for qid, q in spec.questions.items():
            assert q.type in QUESTION_TYPES, f"{point}.{qid} has type {q.type}"
            assert q.instructions or (q.instructions_menu and q.instructions_conversation), f"{point}.{qid}"
            if q.type in ("choice", "score"):
                assert q.criteria or q.criteria_from, f"{point}.{qid} needs criteria or criteria_from"
        policy = registry.policy(point)
        assert policy, f"{point} has no policy.yaml entry"
        assert any(0.0 < t <= 1.0 for t in _thresholds(policy)), f"{point} policy has no threshold"


def test_ac46_call_round_has_exactly_the_four_questions(skills_dir: Path) -> None:
    registry = _registry(require_skills_dir(skills_dir))
    questions = registry.point("call_round").questions
    assert {qid: q.type for qid, q in questions.items()} == CALL_ROUND_QUESTIONS


def test_ac46_question_files_cover_each_point_once_with_allowed_types(skills_dir: Path) -> None:
    index = points_index(require_skills_dir(skills_dir))
    assert set(index) == set(DECISION_POINT_CODES)
    for point, homes in index.items():
        assert len(homes) == 1, f"{point} defined in {[h[0] for h in homes]}"
        for qid, q in homes[0][1]["questions"].items():
            assert q["type"] in QUESTION_TYPES, f"{point}.{qid}: {q['type']}"
    assert set(DECISION_POINT_CODES) <= set(policy_index(skills_dir))


def test_ac46_question_ids_are_globally_unique_and_map_back_to_points(skills_dir: Path) -> None:
    """A-06：录制器与回放器靠题名反推决策点（registry.point_of，待骨架 r2）。"""
    registry = _registry(require_skills_dir(skills_dir))
    seen: dict[str, str] = {}
    for point in DECISION_POINT_CODES:
        qids = set(registry.point(point).questions)
        for qid in qids:
            assert qid not in seen, f"question id {qid} used by both {seen[qid]} and {point}"
            seen[qid] = point
        assert registry.point_of(qids) == point
    assert registry.point_of(set(CALL_ROUND_QUESTIONS)) == "call_round"


# §6.6 对话轮应答意图表的「动作」列（缺口 G-G-06 要求 call_round 的 actions 照它）
INTENT_ACTIONS = {
    "open_answer": "release",
    "decline_offer": "say",
    "state_ai_identity": "say",
    "request_handoff": "handoff",
    "ask_user": "ask_user",
    "confirm_and_end": "end",
    "ask_repeat": "say",
    "wait": "wait",
}


def test_ac46_every_point_has_an_actions_table(skills_dir: Path) -> None:
    """缺口 G-G-06（定稿 add_actions_tables）：每个点的 policy 条目都有非空的 actions 表。"""
    registry = _registry(require_skills_dir(skills_dir))
    for point in DECISION_POINT_CODES:
        actions = registry.policy(point).get("actions")
        assert isinstance(actions, dict) and actions, f"{point} policy has no actions table"
    round_actions = registry.policy("call_round")["actions"]
    for intent, action in INTENT_ACTIONS.items():
        assert round_actions.get(intent) == action, f"call_round actions[{intent}]"
    said = registry.policy("said_as_decided")
    assert said["actions"]["said_ok"] == "wait"
    assert said["actions"]["reinject"] == "say"
    assert said["actions"]["safe_default"] == said["safe_default"]


# ---------------------------------------------------------------------------
# 不合规的副本
# ---------------------------------------------------------------------------


def _home(root: Path, point: str) -> Path:
    for f in sorted(root.glob("*/jev/questions.yaml")):
        if point in ((read_yaml(f) or {}).get("decision_points") or {}):
            return f
    raise AssertionError(f"no questions.yaml defines {point}")


def _first_qid(doc: dict, point: str) -> str:
    return next(iter(doc["decision_points"][point]["questions"]))


def _drop_point(doc: dict) -> None:
    doc["decision_points"].pop("result_item_kind")


def _text_type(doc: dict) -> None:
    q = doc["decision_points"]["call_outcome"]["questions"][_first_qid(doc, "call_outcome")]
    q["type"] = "text"
    q.pop("criteria", None)


def _three_questions(doc: dict) -> None:
    doc["decision_points"]["call_round"]["questions"].pop("stakes")


def _five_questions(doc: dict) -> None:
    doc["decision_points"]["call_round"]["questions"]["summary_ok"] = {
        "type": "noul",
        "instructions": "Is the call going well?",
    }


def _wrong_type(doc: dict) -> None:
    doc["decision_points"]["call_round"]["questions"]["stakes"] = {
        "type": "choice",
        "instructions": "How serious would a wrong answer be?",
        "criteria": {"low": "Low", "high": "High"},
    }


def _choice_without_criteria(doc: dict) -> None:
    q = doc["decision_points"]["call_round"]["questions"]["call_phase"]
    q.pop("criteria", None)
    q.pop("criteria_from", None)


QUESTION_MUTATIONS = {
    "missing_point": ("result_item_kind", _drop_point),
    "text_question_type": ("call_outcome", _text_type),
    "call_round_three_questions": ("call_round", _three_questions),
    "call_round_five_questions": ("call_round", _five_questions),
    "call_round_wrong_type": ("call_round", _wrong_type),
    "choice_without_criteria": ("call_round", _choice_without_criteria),
}


@pytest.mark.parametrize("case", sorted(QUESTION_MUTATIONS))
def test_ac46_validate_rejects_bad_questions(skills_dir: Path, tmp_path: Path, case: str) -> None:
    root = copy_skills(skills_dir, tmp_path)
    point, mutate = QUESTION_MUTATIONS[case]
    edit_yaml(_home(root, point), mutate)
    with pytest.raises(RegistryError):
        load_registry(load_skills(root)).validate()


def test_ac46_validate_rejects_a_point_defined_twice(skills_dir: Path, tmp_path: Path) -> None:
    root = copy_skills(skills_dir, tmp_path)
    home = _home(root, "call_round")
    call_round = read_yaml(home)["decision_points"]["call_round"]
    other = next(f for f in sorted(root.glob("*/jev/questions.yaml")) if f != home)
    edit_yaml(other, lambda doc: doc["decision_points"].__setitem__("call_round", call_round))
    with pytest.raises(RegistryError):
        load_registry(load_skills(root)).validate()


def test_ac46_validate_rejects_a_point_without_policy(skills_dir: Path, tmp_path: Path) -> None:
    root = copy_skills(skills_dir, tmp_path)
    policy_file = next(
        f for f in sorted(root.glob("*/policy.yaml")) if "result_item_kind" in ((read_yaml(f) or {}).get("decision_points") or {})
    )
    edit_yaml(policy_file, lambda doc: doc["decision_points"].pop("result_item_kind"))
    with pytest.raises((RegistryError, PolicyError)):
        load_registry(load_skills(root)).validate()


def test_ac46_validate_rejects_a_point_without_actions(skills_dir: Path, tmp_path: Path) -> None:
    root = copy_skills(skills_dir, tmp_path)
    policy_file = next(
        f for f in sorted(root.glob("*/policy.yaml")) if "task_intent" in ((read_yaml(f) or {}).get("decision_points") or {})
    )
    edit_yaml(policy_file, lambda doc: doc["decision_points"]["task_intent"].pop("actions", None))
    with pytest.raises((RegistryError, PolicyError)):
        load_registry(load_skills(root)).validate()
