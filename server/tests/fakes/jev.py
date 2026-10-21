"""FakeJev：按脚本应答的 Jev 客户端（tech §12；实现 waitless.interfaces.JevClient）。

脚本条目可以是：
- JevResponse，或带 "answers" 键的完整响应字典
- {qid: 答案}：答案是 jev_choice()/jev_noul()/jev_score() 的结果、JevAnswer 或字典；
  choice 没给 probabilities 时按 criteria 自动补齐（选中项 = confidence，其余均分）
- 可调用对象 f(state, questions, point) → 以上任一种
- 异常实例：ask() 时抛出（模拟超时、网络错误）

歧义表 A-06（定稿 infer_from_questions）：JevClient.ask(state, questions) 不带决策点。
FakeJev 记录的 point 由构造参数 point_of（通常传 registry.point_of）按题名集合反推；没给 point_of 时为 None。
ask() 仍接受旧的 point= 关键字（只为兼容已写好的测试；被测代码不应再传），给了就以它为准。

取条目的顺序：按题目 id 集合登记的 on() 队列 → push() 的通用队列 → default → 报错。
每次应答都检查答案与题目一一对应、类型一致、choice 落在 criteria 里；不对就 AssertionError
（这是测试脚本写错，不是被测代码的错）。
"""

from collections import deque
from collections.abc import Callable, Iterable, Mapping
from dataclasses import dataclass
from typing import Any

from waitless.models import JevAnswer, JevQuestion, JevResponse, JevState

Entry = Any


def jev_choice(choice: str, confidence: float = 0.9, probabilities: Mapping[str, float] | None = None) -> dict:
    return {
        "type": "choice",
        "choice": choice,
        "confidence": confidence,
        "probabilities": dict(probabilities) if probabilities is not None else None,
    }


def jev_noul(p: float) -> dict:
    return {"type": "noul", "noul": p}


def jev_score(score: float, confidence: float = 0.9, probabilities: Mapping[str, float] | None = None) -> dict:
    return {
        "type": "score",
        "score": score,
        "confidence": confidence,
        "probabilities": dict(probabilities) if probabilities is not None else None,
    }


def round_answers(
    menu_choice: str,
    *,
    menu_confidence: float = 0.9,
    call_phase: str = "ivr_menu",
    phase_confidence: float = 0.9,
    needs_user: float = 0.05,
    stakes: float = 0,
    stakes_confidence: float = 0.9,
) -> dict[str, dict]:
    """DP-C1 call_round 4 题的一组答案。"""
    return {
        "menu_choice": jev_choice(menu_choice, menu_confidence),
        "call_phase": jev_choice(call_phase, phase_confidence),
        "needs_user": jev_noul(needs_user),
        "stakes": jev_score(stakes, stakes_confidence),
    }


@dataclass
class JevCall:
    """一次 ask：point 是 point_of 反推（或旧的 point= 关键字）的结果，可能为 None。"""

    state: JevState
    questions: dict[str, JevQuestion]
    point: str | None
    response: JevResponse


class FakeJev:
    def __init__(
        self,
        script: Iterable[Entry] = (),
        *,
        default: Entry | None = None,
        model: str = "fake-jev",
        point_of: Callable[[Iterable[str]], str | None] | None = None,
    ) -> None:
        self._queue: deque[Entry] = deque(script)
        self.point_of = point_of
        self._by_questions: dict[frozenset[str], deque[Entry]] = {}
        self.default = default
        self.model = model
        self.calls: list[JevCall] = []

    # -- 脚本 ---------------------------------------------------------------
    def push(self, *entries: Entry) -> "FakeJev":
        self._queue.extend(entries)
        return self

    def on(self, question_ids: str | Iterable[str], *entries: Entry) -> "FakeJev":
        key = frozenset([question_ids] if isinstance(question_ids, str) else question_ids)
        self._by_questions.setdefault(key, deque()).extend(entries)
        return self

    @property
    def remaining(self) -> int:
        return len(self._queue) + sum(len(q) for q in self._by_questions.values())

    def calls_for(self, question_id: str) -> list[JevCall]:
        return [c for c in self.calls if question_id in c.questions]

    # -- JevClient ----------------------------------------------------------
    async def ask(
        self,
        state: JevState | Mapping[str, Any],
        questions: Mapping[str, JevQuestion | Mapping[str, Any]],
        *,
        point: str | None = None,
    ) -> JevResponse:
        st = state if isinstance(state, JevState) else JevState.model_validate(state)
        qs = {
            qid: q if isinstance(q, JevQuestion) else JevQuestion.model_validate(q)
            for qid, q in questions.items()
        }
        if not qs:
            raise AssertionError("FakeJev.ask called with no questions")
        if point is None and self.point_of is not None:
            point = self.point_of(list(qs))
        entry = self._next_entry(frozenset(qs))
        if isinstance(entry, BaseException):
            raise entry
        if callable(entry):
            entry = entry(st, qs, point)
        response = self._complete(entry, qs)
        self._check(response, qs)
        self.calls.append(JevCall(state=st, questions=qs, point=point, response=response))
        return response

    # -- 内部 ---------------------------------------------------------------
    def _next_entry(self, key: frozenset[str]) -> Entry:
        q = self._by_questions.get(key)
        if q:
            return q.popleft()
        if self._queue:
            return self._queue.popleft()
        if self.default is not None:
            return self.default
        raise AssertionError(f"FakeJev script exhausted for questions {sorted(key)}")

    def _complete(self, entry: Entry, qs: dict[str, JevQuestion]) -> JevResponse:
        if isinstance(entry, JevResponse):
            return entry
        if not isinstance(entry, Mapping):
            raise AssertionError(f"FakeJev entry must be a mapping or JevResponse, got {type(entry).__name__}")
        if "answers" in entry:
            return JevResponse.model_validate(entry)
        answers: dict[str, JevAnswer] = {}
        for qid, spec in entry.items():
            if qid not in qs:
                raise AssertionError(f"FakeJev answer for unknown question {qid!r}; asked {sorted(qs)}")
            if isinstance(spec, JevAnswer):
                answers[qid] = spec
                continue
            data = dict(spec)
            data.setdefault("type", qs[qid].type)
            if data["type"] == "choice" and data.get("probabilities") is None:
                data["probabilities"] = _spread(data["choice"], data.get("confidence", 0.9), qs[qid])
            answers[qid] = JevAnswer.model_validate({k: v for k, v in data.items() if v is not None})
        return JevResponse(answers=answers, model=self.model, usage={"input_tokens": 0, "output_tokens": 0})

    @staticmethod
    def _check(response: JevResponse, qs: dict[str, JevQuestion]) -> None:
        if set(response.answers) != set(qs):
            raise AssertionError(
                f"FakeJev answers {sorted(response.answers)} do not match questions {sorted(qs)}"
            )
        for qid, ans in response.answers.items():
            q = qs[qid]
            if ans.type != q.type:
                raise AssertionError(f"{qid}: answer type {ans.type} != question type {q.type}")
            if q.type == "choice" and isinstance(q.criteria, dict):
                if ans.choice not in q.criteria:
                    raise AssertionError(f"{qid}: choice {ans.choice!r} not in criteria {sorted(q.criteria)}")
                extra = set(ans.probabilities or {}) - set(q.criteria)
                if extra:
                    raise AssertionError(f"{qid}: probabilities for unknown criteria {sorted(extra)}")


def _spread(choice: str, confidence: float, question: JevQuestion) -> dict[str, float]:
    keys = list(question.criteria) if isinstance(question.criteria, dict) else [choice]
    if choice not in keys:
        keys.append(choice)
    if len(keys) == 1:
        return {choice: 1.0}
    rest = (1.0 - confidence) / (len(keys) - 1)
    return {k: (confidence if k == choice else rest) for k in keys}


__all__ = ["FakeJev", "JevCall", "jev_choice", "jev_noul", "jev_score", "round_answers"]
