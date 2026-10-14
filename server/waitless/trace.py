"""trace 事件写入、检查与指标（A 线；tech §6.11；AC-34、AC-35、AC-75、AC-96）。

公开接口（§7）：Trace.event(...) -> seq、check_call(call_id) -> [Violation]、metrics(task_id)。
歧义表 A-24（定稿 methods_callresult）：check_call、metrics（以及 menu_to_dtmf_ms）是 Trace 的方法。
事件里放什么见 models.TraceEvent 的文档与 models.TRACE_ACTION_KEYS（A-08）。
"""

from waitless.models import CONSEQUENTIAL_EVENTS, DECISION_EVENT_REQUIRED

from pydantic import BaseModel

from waitless.interfaces import Clock
from waitless.models import TraceEvent
from waitless.store import Store

__all__ = ["Trace", "TraceMetrics", "Violation"]


class Violation(BaseModel):
    """一条不合格：有后果的事件没有 decision_id 或找不到对应的 decision 事件，或 decision 事件不完整。"""

    seq: int
    event_type: str
    decision_id: str | None
    reason: str


class TraceMetrics(BaseModel):
    """AC-35：interrupts_used = pending_choice_created + handoff_ready；
    user_active_s = Σ(acted_ms − shown_ms)，只算任务卡确认与待决选项作答，不含转接期间。"""

    task_id: str
    interrupts_used: int
    user_active_s: float
    menu_to_dtmf_ms: list[int]


class Trace:
    """实现 interfaces.Trace。"""

    def __init__(self, store: Store, clock: Clock) -> None:
        self.store = store
        self.clock = clock

    def event(self, ev: TraceEvent) -> int:
        """写入事件；t_ms 为空时取 clock.monotonic_ms()；返回 seq。

        歧义表 A-25（定稿 keep_all_relax_trace_event）：写入时检查，不合格抛 ValueError、什么都不写：
          - ev.event_type ∈ models.CONSEQUENTIAL_EVENTS 而 decision_id 为空；
          - decision 事件缺 models.DECISION_EVENT_REQUIRED 任一项；
          - 歧义表 A-08：decision_point=call_round 的 decision 事件，action 里没有整数 turn_seq。
        这些检查原来在 TraceEvent 模型里，现在只在这里；读回不校验，坏行交给 check_call 报告。
        """
        if ev.event_type in CONSEQUENTIAL_EVENTS and not ev.decision_id:
            raise ValueError("consequential event requires decision_id")
        if ev.event_type == "decision":
            if any(getattr(ev, key) is None for key in DECISION_EVENT_REQUIRED):
                raise ValueError("incomplete decision event")
            if ev.decision_point == "call_round" and type((ev.action or {}).get("turn_seq")) is not int:
                raise ValueError("call_round requires integer turn_seq")
        if ev.call_id and ev.event_type in ("company_turn", "waitless_utterance"):
            active = False
            for prior in self.events(call_id=ev.call_id):
                if prior.event_type == "handoff_started":
                    active = True
                elif prior.event_type == "handoff_ended":
                    active = False
            if active:
                raise ValueError("transcript prohibited during handoff")
        if ev.t_ms is None:
            ev = ev.model_copy(update={"t_ms": self.clock.monotonic_ms()})
        return self.store.insert_turn(ev)

    def has_decision(self, decision_id: str) -> bool:
        return any(ev.event_type == "decision" and ev.decision_id == decision_id for ev in self.events())

    def events(self, *, call_id: str | None = None, task_id: str | None = None) -> list[TraceEvent]:
        """读回事件（不做 A-25 的写入检查，所以直接写进 turns 的坏行也能读出来）。"""
        return self.store.list_turns(call_id=call_id, task_id=task_id)

    def check_call(self, call_id: str) -> list[Violation]:
        """遍历一通电话的有后果事件（CONSEQUENTIAL_EVENTS）：没有 decision_id、按 decision_id 找不到 decision 事件、
        或那条 decision 事件缺 DECISION_EVENT_REQUIRED，都记一条 Violation（AC-34）。空列表表示合格。
        AC-34 的测试经 Store.insert_turn 直接写坏行（绕过 event() 的检查）来构造不完整的 trace。"""
        events = self.events(call_id=call_id)
        decisions = {}
        violations = []
        for ev in events:
            if ev.event_type == "decision" and ev.decision_id:
                decisions[ev.decision_id] = ev
            reason = None
            if ev.event_type == "decision" and any(getattr(ev, k) is None for k in DECISION_EVENT_REQUIRED):
                reason = "incomplete decision"
            elif ev.event_type in CONSEQUENTIAL_EVENTS:
                decision = decisions.get(ev.decision_id)
                if not ev.decision_id:
                    reason = "missing decision_id"
                elif decision is None:
                    reason = "missing corresponding decision"
                elif any(getattr(decision, k) is None for k in DECISION_EVENT_REQUIRED):
                    reason = "incomplete corresponding decision"
            if reason:
                violations.append(Violation(seq=ev.seq, event_type=ev.event_type, decision_id=ev.decision_id, reason=reason))
        return violations

    def metrics(self, task_id: str) -> TraceMetrics:
        events = self.events(task_id=task_id)
        interrupts = sum(e.event_type in ("pending_choice_created", "handoff_ready") for e in events)
        active_ms = 0
        for ev in events:
            action = ev.action or {}
            if ev.event_type == "user_action" and action.get("name") in ("start_confirmed", "choice_answered"):
                active_ms += max(0, action["acted_ms"] - action["shown_ms"])
        call_ids = dict.fromkeys(e.call_id for e in events if e.call_id)
        return TraceMetrics(task_id=task_id, interrupts_used=interrupts, user_active_s=active_ms/1000,
                            menu_to_dtmf_ms=[ms for cid in call_ids for ms in self.menu_to_dtmf_ms(cid)])

    def menu_to_dtmf_ms(self, call_id: str) -> list[int]:
        """AC-96，歧义表 A-08（定稿 convention_plus_turn_link）：对每条 dtmf_sent，按 decision_id 找到 decision 事件，
        取它 action.turn_seq，再找同一通 action.seq 等于它、且 action.kind=menu 的 company_turn，
        记 dtmf_sent.t_ms − company_turn.t_ms（company_turn 的 t_ms 是 CompanyTurn.t_end_ms）。
        press_field 等对话轮的按键（kind=speech）不计入；回溯不到的按键也不计入（check_call 另会报它）。"""
        events = self.events(call_id=call_id)
        decisions = {e.decision_id: e for e in events if e.event_type == "decision"}
        menus = {(e.action or {}).get("seq"): e for e in events
                 if e.event_type == "company_turn" and (e.action or {}).get("kind") == "menu"}
        delays = []
        for ev in events:
            if ev.event_type == "dtmf_sent" and (decision := decisions.get(ev.decision_id)):
                menu = menus.get((decision.action or {}).get("turn_seq"))
                if menu is not None and ev.t_ms is not None and menu.t_ms is not None:
                    delays.append(ev.t_ms-menu.t_ms)
        return delays
