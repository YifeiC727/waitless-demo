"""FakeTrace：内存里的 trace（tech §12、§6.11；实现 waitless.interfaces.Trace）。

缺口 G-G-01（定稿 shared_fake_trace）：D、E、G 共用这一个替身，写入检查与 interfaces.Trace.event 的文档一致
（A-25、A-08）。不合格抛 ValueError、不记录：
  1. event_type 属于 CONSEQUENTIAL_EVENTS 而没有 decision_id；
  2. decision 事件缺 DECISION_EVENT_REQUIRED 任一项；
  3. decision_point=call_round 的 decision 事件，action 里没有整数 turn_seq。
seq 从 0 递增；t_ms 为空时取 clock.monotonic_ms()（给了 clock 时），否则取 seq。

只读辅助（r4 按 D 线需要补，检查规则不变）：
  violations     写入时 decision_id 指向的 decision 事件还不存在的有后果事件（照收，另记一笔；AC-34 的口径），
                 测试据此断言每个动作都能追溯到决策
  of_type(*types, call_id=None)、types(call_id=None)、decision(decision_id)、text_dump()
"""

import json
from collections.abc import Mapping
from typing import Any

from waitless.models import CONSEQUENTIAL_EVENTS, DECISION_EVENT_REQUIRED, TURN_SEQ_KEY, TraceEvent


def write_problems(ev: TraceEvent) -> list[str]:
    """Trace.event() 写入时要拒绝的问题（空列表表示可以写）。"""
    problems: list[str] = []
    if ev.event_type in CONSEQUENTIAL_EVENTS and not ev.decision_id:
        problems.append(f"{ev.event_type} must carry a decision_id")
    if ev.event_type == "decision":
        missing = [name for name in DECISION_EVENT_REQUIRED if getattr(ev, name) is None]
        if missing:
            problems.append("decision event needs " + ", ".join(missing))
        if ev.decision_point == "call_round":
            turn_seq = (ev.action or {}).get(TURN_SEQ_KEY)
            if not isinstance(turn_seq, int) or isinstance(turn_seq, bool):
                problems.append("call_round decision event needs action.turn_seq (int)")
    return problems


class FakeTrace:
    def __init__(self, clock: Any = None) -> None:
        self._clock = clock
        self.rows: list[TraceEvent] = []
        self.rejected: list[tuple[TraceEvent, list[str]]] = []
        self.violations: list[TraceEvent] = []

    # -- interfaces.Trace ---------------------------------------------------
    def event(self, ev: TraceEvent | Mapping[str, Any]) -> int:
        row = ev if isinstance(ev, TraceEvent) else TraceEvent.model_validate(ev)
        problems = write_problems(row)
        if problems:
            self.rejected.append((row, problems))
            raise ValueError("; ".join(problems))
        if row.event_type in CONSEQUENTIAL_EVENTS and row.decision_id and not self.has_decision(row.decision_id):
            self.violations.append(row)
        seq = len(self.rows)
        t_ms = row.t_ms
        if t_ms is None:
            t_ms = int(self._clock.monotonic_ms()) if self._clock is not None else seq
        self.rows.append(row.model_copy(update={"seq": seq, "t_ms": t_ms}))
        return seq

    def has_decision(self, decision_id: str) -> bool:
        return any(r.event_type == "decision" and r.decision_id == decision_id for r in self.rows)

    def events(self, *, call_id: str | None = None, task_id: str | None = None) -> list[TraceEvent]:
        return [
            r
            for r in self.rows
            if (call_id is None or r.call_id == call_id) and (task_id is None or r.task_id == task_id)
        ]

    # -- 测试辅助 -----------------------------------------------------------
    @property
    def all(self) -> list[TraceEvent]:
        return list(self.rows)

    def of_type(self, *event_types: str, call_id: str | None = None) -> list[TraceEvent]:
        return [r for r in self.events(call_id=call_id) if r.event_type in event_types]

    def types(self, call_id: str | None = None) -> list[str]:
        return [r.event_type for r in self.events(call_id=call_id)]

    def decision(self, decision_id: str) -> TraceEvent | None:
        return next((r for r in self.rows if r.event_type == "decision" and r.decision_id == decision_id), None)

    def decisions(self) -> list[TraceEvent]:
        return self.of_type("decision")

    def text_dump(self) -> str:
        """全部事件的 JSON（搜诱饵值、转写之类的断言用）。"""
        return json.dumps([r.model_dump(mode="json") for r in self.rows], ensure_ascii=False)


__all__ = ["FakeTrace", "write_problems"]
