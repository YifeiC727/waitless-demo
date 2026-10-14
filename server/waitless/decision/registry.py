"""决策点注册表：合并各 skill 的 jev/questions.yaml 与 policy.yaml 并校验（G 线；tech §6.6；AC-46）。

公开接口（§7）：load_registry(skills).validate()。
"""

from collections.abc import Iterable, Mapping
from typing import Any
import copy
import yaml

from pydantic import BaseModel, ValidationError

from waitless.models import DECISION_POINT_CODES, CALL_ROUND_QUESTIONS, DecisionPoint, Skill

__all__ = ["REQUIRED_POINTS", "PointQuestions", "QuestionSpec", "Registry", "RegistryError", "load_registry"]

REQUIRED_POINTS: tuple[str, ...] = tuple(DECISION_POINT_CODES)


class RegistryError(Exception):
    """某决策点缺 questions 或 policy、题型不是 choice/noul/score、call_round 不是恰好 4 题等。"""


class QuestionSpec(BaseModel):
    """questions.yaml 里的一题（编译前的样子，§6.6 结构）。"""

    type: str
    instructions: str | None = None
    instructions_menu: str | None = None
    instructions_conversation: str | None = None
    criteria: dict[str, str] | list[str] | None = None
    criteria_from: str | None = None
    extra_criteria: dict[str, str] | None = None


class PointQuestions(BaseModel):
    when: str
    questions: dict[str, QuestionSpec]


class Registry:
    """point_id → 题目、策略，以及它们来自哪个 skill。"""

    def __init__(
        self,
        *,
        questions: Mapping[str, PointQuestions],
        policies: Mapping[str, Mapping[str, Any]],
        sources: Mapping[str, str],
    ) -> None:
        self._questions = copy.deepcopy(dict(questions))
        self._policies = copy.deepcopy(dict(policies))
        self._sources = dict(sources)
        self._default_task_goal = ""
        self._global = {}

    def validate(self) -> "Registry":
        """校验（AC-46），合格返回自身，不合格抛 RegistryError：
        - §6.6 的 8 个决策点都在 questions.yaml 里，每题带 type、instructions（或 instructions_menu 与
          instructions_conversation）、criteria 或 criteria_from；每个点都有 policy.yaml 条目（阈值与动作）。
        - 题型只能是 choice、noul、score；没有要求生成文字的题型。
        - call_round 恰含 models.CALL_ROUND_QUESTIONS 的 4 题与题型。
        - 歧义表 A-06（定稿 infer_from_questions）：所有决策点的题名全局唯一（同一题名出现在两个点里就报错），
          这样 point_of() 才能从题名集合反推决策点；以后新增决策点不能复用已有题名。
        - 歧义表 A-26：assisted-call 的 questions.yaml 顶层有非空的 default_task_goal（建卡前的 task_goal）。
        - 缺口 G-G-06（定稿 add_actions_tables）：每个点的 policy 条目都有阈值（min_confidence 或 min_probability）
          和 actions 表（AC-46「含阈值和动作表」）：call_round 把应答意图映射到动作（同 §6.6 意图表），
          said_as_decided {said_ok: wait, reinject: say, safe_default: <safe_default 的动作>}，
          call_outcome、result_item_kind {"*": wait}，其余点按各自 §6.6 的「动作」列。缺任一项抛 RegistryError。"""
        if not set(REQUIRED_POINTS) <= self._questions.keys():
            raise RegistryError("missing required decision points")
        if set(self._questions) != set(self._policies):
            raise RegistryError("questions and policies cover different points")
        if not isinstance(self._default_task_goal, str) or not self._default_task_goal.strip():
            raise RegistryError("missing default_task_goal")
        seen = set()
        for point, spec in self._questions.items():
            if not spec.questions:
                raise RegistryError(f"no questions for {point}")
            for qid, question in spec.questions.items():
                if qid in seen:
                    raise RegistryError(f"duplicate question id: {qid}")
                seen.add(qid)
                if question.type not in {"choice", "noul", "score"}:
                    raise RegistryError(f"invalid question type: {point}.{qid}")
                if not (question.instructions or (question.instructions_menu and question.instructions_conversation)):
                    raise RegistryError(f"missing instructions: {point}.{qid}")
                if question.type in {"choice", "score"} and not (question.criteria or question.criteria_from):
                    raise RegistryError(f"missing criteria: {point}.{qid}")
                if question.type == "choice" and question.criteria is not None and not isinstance(question.criteria, dict):
                    raise RegistryError(f"choice criteria must be a mapping: {point}.{qid}")
                if question.type == "score" and question.criteria is not None and not isinstance(question.criteria, list):
                    raise RegistryError(f"score criteria must be ordered: {point}.{qid}")
            policy = self._policies[point]
            if not isinstance(policy.get("actions"), dict) or not policy["actions"]:
                raise RegistryError(f"missing actions: {point}")
            if not _has_threshold(policy):
                raise RegistryError(f"missing or invalid threshold: {point}")
        if {qid: q.type for qid, q in self._questions["call_round"].questions.items()} != CALL_ROUND_QUESTIONS:
            raise RegistryError("call_round requires exactly its four typed questions")
        from waitless.decision.policy import Policy
        Policy.load(self.policy_raw())
        return self

    def point(self, point: DecisionPoint) -> PointQuestions:
        try:
            return self._questions[point].model_copy(deep=True)
        except KeyError:
            raise RegistryError(f"unknown point: {point}") from None

    def point_of(self, question_ids: Iterable[str]) -> DecisionPoint:
        """A-06：题名集合恰好等于某个决策点的全部题名时返回该点；否则抛 RegistryError
        （E 的 ReplayRecorder 与 ReplayPlayer 用它写、核对 shape.point）。"""
        ids = set(question_ids)
        matches = [point for point, spec in self._questions.items() if set(spec.questions) == ids]
        if len(matches) != 1:
            raise RegistryError("question ids do not identify exactly one decision point")
        return matches[0]

    @property
    def default_task_goal(self) -> str:
        """A-26：建卡前（DecisionContext.task_card 为 null）Jev state.task_goal 用的一句通用目标。"""
        return self._default_task_goal

    def policy(self, point: DecisionPoint) -> Mapping[str, Any]:
        try:
            return copy.deepcopy(self._policies[point])
        except KeyError:
            raise RegistryError(f"unknown point: {point}") from None

    def policy_raw(self) -> dict[str, Any]:
        """缺口 G-G-02（定稿 registry_policy_raw）：合并后的 policy，交给 Policy.load：
        {"version": 1, "global": decide-option 的 global 段, "decision_points": 各 skill 的 decision_points 并集}。
        A 的 build_services 与 E 的装配都写 Policy.load(registry.policy_raw())。"""
        return {"version": 1, "global": copy.deepcopy(self._global),
                "decision_points": copy.deepcopy(self._policies)}


def load_registry(skills: Mapping[str, Skill]) -> Registry:
    """读取全部 skill 的 jev/questions.yaml 与 policy.yaml；同一决策点出现在两个 skill 里也是 RegistryError。"""
    questions, policies, sources = {}, {}, {}
    goal, global_ = "", {}
    try:
        for name, skill in skills.items():
            question_path = skill.path / "jev/questions.yaml"
            if question_path.is_file():
                raw = yaml.safe_load(question_path.read_text(encoding="utf-8"))
                if raw.get("version") != 1:
                    raise RegistryError("unsupported questions version")
                if name == "assisted-call":
                    goal = raw.get("default_task_goal", "")
                for point, spec in raw.get("decision_points", {}).items():
                    if point in questions:
                        raise RegistryError(f"duplicate decision point: {point}")
                    questions[point] = PointQuestions.model_validate(spec)
                    sources[point] = name
            policy_path = skill.path / "policy.yaml"
            if policy_path.is_file():
                raw = yaml.safe_load(policy_path.read_text(encoding="utf-8"))
                if raw.get("version") != 1:
                    raise RegistryError("unsupported policy version")
                if name == "decide-option":
                    global_ = raw.get("global", {})
                for point, spec in raw.get("decision_points", {}).items():
                    if point in policies:
                        raise RegistryError(f"duplicate policy point: {point}")
                    policies[point] = spec
    except (OSError, ValueError, TypeError, yaml.YAMLError, ValidationError) as exc:
        raise RegistryError("invalid registry files") from exc
    registry = Registry(questions=questions, policies=policies, sources=sources)
    registry._default_task_goal, registry._global = goal, global_
    return registry.validate()


def _has_threshold(value) -> bool:
    if isinstance(value, dict):
        return any((k in {"min_confidence", "min_probability"} and isinstance(v, (float, int))
                    and not isinstance(v, bool) and 0 < v <= 1) or _has_threshold(v)
                   for k, v in value.items())
    return False
