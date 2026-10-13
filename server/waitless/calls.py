"""CallManager：按模式建 LiveCall 或 TextCall，实现 MCP 需要的通话接口（A 线；tech §6.1、§7；AC-24、AC-25、AC-94）。

公开接口（§7）：CallManager.start(task_id, confirmed, card_version)、.status(task_id, debug)、
.answer(task_id, choice_id, option_id)、.join(task_id)、.return_(task_id, handoff_id)。
它也实现 interfaces.CallRegistry（D 线的 WebSocket 端点按 call_id 找通话）。
"""

from collections.abc import Callable

from waitless.config import Settings
from waitless.interfaces import CallDeps, CallSession, CallSessionFactory, Store, Trace
from waitless.results import ResultService
from waitless.models import (
    AnswerChoiceOut,
    CallStatusOut,
    CompiledCall,
    JoinCallOut,
    ReturnOut,
    StartCallOut,
)

__all__ = ["CallManager"]


class CallManager:
    def __init__(
        self,
        *,
        settings: Settings,
        store: Store,
        trace: Trace,
        compile_call: Callable[..., CompiledCall],
        session_factory: CallSessionFactory,
        deps: CallDeps,
        results: ResultService,
        replay_scenario_id: str | None = None,
    ) -> None:
        """replay_scenario_id：replay 模式下由 app 组装时传入 ReplayPlayer.header.scenario_id（A-11）。
        results：缺口 A-G-8（定稿 results_kwarg），on_ended 里 await results.finalize(call_id)。"""
        raise NotImplementedError

    async def start(self, task_id: str, confirmed: bool, card_version: int, *, scenario_id: str | None = None) -> StartCallOut:
        """两道守门之一（R13）：先核对 confirmed 与 card_version，再写库或拨号（AC-24）。"""
        # todo(A): confirmed 不是 True → ToolFailure("start_not_confirmed")；card_version 不是当前版本 →
        #          ToolFailure("card_version_mismatch")；两种情况 tasks 与 calls 表都不变。
        # todo(A): 任务 unsupported → "unsupported"（AC-27）；不是 ready → "task_not_ready"（含官方号码未核实的
        #          setup_required 任务，A-14）；已有进行中的通话 → "call_in_progress"。
        # todo(A): compile_call(task_card, skills, profile, rules, prefs) → CallSpec → session_factory(spec, deps) → start()。
        # todo(A): 写 calls 行（mode、skill_commits_json）、user_action(start_confirmed, shown_ms, acted_ms) 事件；
        #          tasks.status=in_call。一个任务可以有多通（AC-25）。
        # todo(A): 缺口 A-G-9（定稿 server_timestamps）：shown_ms = 该版任务卡以 ready 返回的时刻（TaskService 记在 tasks 行），
        #          acted_ms = 本次调用的时刻，都按 clock.now_iso() 换算成毫秒级 Unix 时间；只有 CallManager 写这类事件。
        # todo(A): 未知 task_id → ToolFailure("unknown_task")（缺口 R2-1），在核对 confirmed 之前。
        # todo(A): CallSpec.scenario_id 按 A-11 定：text-sim 用参数 scenario_id，缺省用
        #          settings.engine.textsim.default_scenario；replay 一律用 replay_scenario_id（参数另给且不同时
        #          报错，不悄悄换）；live-sim 为 None。
        # todo(A): session.start() 返回后读 session.scenario_id 写进 calls 行（live-sim 是拨号前 GET 的结果，
        #          失败为 null，A-10）。
        raise NotImplementedError

    def status(self, task_id: str, debug: bool = False) -> CallStatusOut:
        """没有通话时 call_id=null、phase=idle；未知 task → ToolFailure("unknown_task")。
        debug=True 时 last_decision 按 task_id 取本任务最近一条决策（含 DP-A1、P1–P3），
        任务还没有任何决策时才为 null（A-09）：有通话时交给 session.status(debug)（它同样按 task_id 查 trace），
        还没有通话时这里自己按 task_id 查 trace。"""
        raise NotImplementedError

    async def answer(self, task_id: str, choice_id: str, option_id: str) -> AnswerChoiceOut:
        """未知 task → ToolFailure("unknown_task")（A-18）；其余错误码由 session / PendingRegistry 抛。
        作答被记录（choice_recorded）时写 user_action(choice_answered)：shown_ms = PendingChoice.created_at，
        acted_ms = 本次调用的时刻（缺口 A-G-9，D、E 不写这类事件）。"""
        raise NotImplementedError

    async def join(self, task_id: str) -> JoinCallOut:
        """未知 task → ToolFailure("unknown_task")（A-18）；没有通话或未到 handoff_ready → "handoff_not_ready"。"""
        raise NotImplementedError

    async def return_(self, task_id: str, handoff_id: str) -> ReturnOut:
        """AC-78：交给 session.return_to_agent，状态变 resuming。
        未知 task → ToolFailure("unknown_task")（A-18）；没有进行中的转接 → "no_active_handoff"。"""
        raise NotImplementedError

    def get_session(self, call_id: str) -> CallSession | None:
        raise NotImplementedError

    async def on_ended(self, call_id: str) -> None:
        """CallDeps.on_ended 指向这里：await self.results.finalize(call_id) 得到 CallResult（A-24、A-G-8），
        按 CallResult.outcome 更新 calls 行与 tasks.status。"""
        raise NotImplementedError

    async def shutdown(self) -> None:
        raise NotImplementedError

