"""trace 事件写入、检查与指标（A 线；tech §6.11；AC-34、AC-35、AC-75、AC-96）。

公开接口（§7）：Trace.event(...) -> seq、check_call(call_id) -> [Violation]、metrics(task_id)。
歧义表 A-24（定稿 methods_callresult）：check_call、metrics（以及 menu_to_dtmf_ms）是 Trace 的方法。
事件里放什么见 models.TraceEvent 的文档与 models.TRACE_ACTION_KEYS（A-08）。
"""

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
        raise NotImplementedError

    def event(self, ev: TraceEvent) -> int:
        """写入事件；t_ms 为空时取 clock.monotonic_ms()；返回 seq。

        歧义表 A-25（定稿 keep_all_relax_trace_event）：写入时检查，不合格抛 ValueError、什么都不写：
          - ev.event_type ∈ models.CONSEQUENTIAL_EVENTS 而 decision_id 为空；
          - decision 事件缺 models.DECISION_EVENT_REQUIRED 任一项；
          - 歧义表 A-08：decision_point=call_round 的 decision 事件，action 里没有整数 turn_seq。
        这些检查原来在 TraceEvent 模型里，现在只在这里；读回不校验，坏行交给 check_call 报告。
        """
        # todo(A): 转接期间（handoff_started 与 handoff_ended 之间）不写 company_turn / waitless_utterance
        #          —— 主要由 D 线的 TranscriptTap 保证，这里再拒一次（AC-75）。
        raise NotImplementedError

    def has_decision(self, decision_id: str) -> bool:
        raise NotImplementedError

    def events(self, *, call_id: str | None = None, task_id: str | None = None) -> list[TraceEvent]:
        """读回事件（不做 A-25 的写入检查，所以直接写进 turns 的坏行也能读出来）。"""
        raise NotImplementedError

    def check_call(self, call_id: str) -> list[Violation]:
        """遍历一通电话的有后果事件（CONSEQUENTIAL_EVENTS）：没有 decision_id、按 decision_id 找不到 decision 事件、
        或那条 decision 事件缺 DECISION_EVENT_REQUIRED，都记一条 Violation（AC-34）。空列表表示合格。
        AC-34 的测试经 Store.insert_turn 直接写坏行（绕过 event() 的检查）来构造不完整的 trace。"""
        raise NotImplementedError

    def metrics(self, task_id: str) -> TraceMetrics:
        raise NotImplementedError

    def menu_to_dtmf_ms(self, call_id: str) -> list[int]:
        """AC-96，歧义表 A-08（定稿 convention_plus_turn_link）：对每条 dtmf_sent，按 decision_id 找到 decision 事件，
        取它 action.turn_seq，再找同一通 action.seq 等于它、且 action.kind=menu 的 company_turn，
        记 dtmf_sent.t_ms − company_turn.t_ms（company_turn 的 t_ms 是 CompanyTurn.t_end_ms）。
        press_field 等对话轮的按键（kind=speech）不计入；回溯不到的按键也不计入（check_call 另会报它）。"""
        raise NotImplementedError
