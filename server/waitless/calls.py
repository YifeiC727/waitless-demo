"""CallManager：按模式建 LiveCall 或 TextCall，实现 MCP 需要的通话接口（A 线；tech §6.1、§7；AC-24、AC-25、AC-94）。

公开接口（§7）：CallManager.start(task_id, confirmed, card_version)、.status(task_id, debug)、
.answer(task_id, choice_id, option_id)、.join(task_id)、.listen(task_id)、.return_(task_id, handoff_id)。
它也实现 interfaces.CallRegistry（D 线的 WebSocket 端点按 call_id 找通话）。
"""

import asyncio
import inspect
from waitless.call.listen import ListenTicketOut
from waitless.tasks import line_display_name, _card_for_mode
import uuid
from datetime import datetime, timedelta
from waitless.interfaces import ToolFailure
from waitless.models import CallSpec, TaskCard, TraceEvent, CallStatusDebug, GuardCounters, LastDecision

from collections.abc import Callable

from waitless.config import Settings
from waitless.interfaces import CallDeps, CallSession, CallSessionFactory, Store, Trace
from waitless.results import ResultService
from waitless.models import (
    AnswerChoiceOut,
    CallStatusOut,
    CallResult,
    CompiledCall,
    JoinCallOut,
    ReturnOut,
    StartCallOut,
    HangupCallOut,
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
        self.settings, self.store, self.trace = settings, store, trace
        self.compile_call, self.session_factory, self.deps, self.results = compile_call, session_factory, deps, results
        self.replay_scenario_id = replay_scenario_id
        self._sessions = {}
        self._locks = {}
        self.deps.on_ended = self.on_ended
        self.deps.on_handoff_connected = self.handoff_connected
        self._hangup_locks = {}
        self._remote_hangups = {}

    async def start(self, task_id: str, confirmed: bool, card_version: int, *, scenario_id: str | None = None) -> StartCallOut:
        """两道守门之一（R13）：先核对 confirmed 与 card_version，再写库或拨号（AC-24）。"""
        lock = self._locks.setdefault(task_id, asyncio.Lock())
        async with lock:
            row = self._task(task_id)
            if confirmed is not True:
                raise ToolFailure("start_not_confirmed")
            if type(card_version) is not int or card_version != row["card_version"]:
                raise ToolFailure("card_version_mismatch")
            if row["status"] == "unsupported":
                raise ToolFailure("unsupported")
            if (any(not s.ended and s.task_id == task_id for s in self._sessions.values())
                    or any(not r["ended_at"] for r in self.store.list_calls(task_id))):
                raise ToolFailure("call_in_progress")
            if row["status"] != "ready":
                raise ToolFailure("task_not_ready")
            card = _card_for_mode(TaskCard.model_validate_json(row["task_card_json"]),
                                  self.results.skills, self.settings.mode)
            if any(q.needs_user or q.selected is None for q in card.precall_decisions):
                raise ToolFailure("task_not_ready")
            selected_scenario = scenario_id or self.settings.engine.textsim.default_scenario
            if self.settings.mode == "replay":
                if not self.replay_scenario_id:
                    raise ValueError("Replay scenario is missing")
                if scenario_id is not None and scenario_id != self.replay_scenario_id:
                    raise ValueError("Replay scenario differs from recording")
                selected_scenario = self.replay_scenario_id
            elif self.settings.mode == "live-sim":
                selected_scenario = None
            compile_options = {"mode": self.settings.mode} if "mode" in inspect.signature(self.compile_call).parameters else {}
            compiled = self.compile_call(card, self.results.skills, self.store.get_profile(),
                                         self.store.list_standing_rules(), self.store.list_preferences(row["company_id"]),
                                         **compile_options)
            call_id = uuid.uuid4().hex
            if hasattr(self, "_bind_backend"):
                self._bind_backend(task_id=task_id, call_id=call_id)
            company = self.store.get_company(card.company.company_id)
            if company is None and card.simulated_line is not None:
                raise ValueError("simulated line is not configured")
            spec = CallSpec(call_id=call_id, task_id=task_id, mode=self.settings.mode, company_name=line_display_name(card),
                            to_number=company["line_number"] if company else card.company.official_number,
                            scenario_id=selected_scenario, compiled=compiled)
            session = self.session_factory(spec, self.deps)
            now = self.deps.clock.now_iso()
            self.store.create_call(call_id=call_id, task_id=task_id, scenario_id=selected_scenario, mode=self.settings.mode,
                                   started_at=now, skill_commits=[{"name": s.name, "commit": s.commit} for s in compiled.skill_versions])
            self._sessions[call_id] = session
            self.store.update_task(task_id, status="in_call", updated_at=now)
            self.trace.event(TraceEvent(task_id=task_id, call_id=call_id, event_type="user_action", speaker="user",
                            action={"name": "start_confirmed", "shown_ms": _unix_ms(row["updated_at"]), "acted_ms": _unix_ms(now)}))
            try:
                await session.start()
                self.store.update_call(call_id, scenario_id=session.scenario_id)
            except Exception:
                self.store.update_call(call_id, ended_at=self.deps.clock.now_iso(), outcome="failed")
                self.store.update_task(task_id, status="failed", updated_at=self.deps.clock.now_iso())
                raise
            return StartCallOut(task_id=task_id, call_id=call_id, status="dialing")

    def status(self, task_id: str, debug: bool = False) -> CallStatusOut:
        """没有通话时 call_id=null、phase=idle；未知 task → ToolFailure("unknown_task")。
        debug=True 时 last_decision 按 task_id 取本任务最近一条决策（含 DP-A1、P1–P3），
        任务还没有任何决策时才为 null（A-09）：有通话时交给 session.status(debug)（它同样按 task_id 查 trace），
        还没有通话时这里自己按 task_id 查 trace。"""
        self._task(task_id)
        session = self._latest_session(task_id)
        if session:
            out = session.status(debug)
            if out.ended:
                row = next((r for r in self.store.list_calls(task_id) if r["call_id"] == session.call_id), None)
                if row is None or not row["ended_at"] or not row["result_json"]:
                    out = out.model_copy(update={"ended": False, "phase": "wrapping_up",
                                                 "status_line": "Preparing the call result."})
        else:
            out = CallStatusOut(task_id=task_id, call_id=None, phase="idle", status_line="Review your task card.",
                                 transcript_tail=[], pending_choice=None, handoff=None, interrupts_used=0,
                                 elapsed_s=0.0, ended=False, debug=None)
        if debug:
            last = next((e for e in reversed(self.trace.events(task_id=task_id)) if e.event_type == "decision"), None)
            last_decision = None
            if last:
                policy = last.policy_decision or {}
                last_decision = LastDecision(decision_id=last.decision_id, point=last.decision_point,
                             intent=policy.get("intent", ""), action=policy.get("action", "wait"),
                             reason_code=policy.get("reason_code", ""), jev=(last.jev_response or {}).get("answers", {}),
                             latency_ms=last.latency_ms or 0)
            details = out.debug or CallStatusDebug(mode=self.settings.mode,
                                                  skills=[{"name": s.name, "commit": s.version.commit}
                                                          for s in self.results.skills.values()], last_decision=None,
                                                  guard=GuardCounters(blocked=0, released=0))
            out = out.model_copy(update={"debug": details.model_copy(update={"last_decision": last_decision})})
        return out

    async def answer(self, task_id: str, choice_id: str, option_id: str) -> AnswerChoiceOut:
        """未知 task → ToolFailure("unknown_task")（A-18）；其余错误码由 session / PendingRegistry 抛。
        作答被记录（choice_recorded）时写 user_action(choice_answered)：shown_ms = PendingChoice.created_at，
        acted_ms = 本次调用的时刻（缺口 A-G-9，D、E 不写这类事件）。"""
        session = self._require_session(task_id, "unknown_choice")
        pending = self.deps.pending.get(choice_id)
        if pending is None or self.deps.pending.current(session.call_id) is None or self.deps.pending.current(session.call_id).choice_id != choice_id:
            raise ToolFailure("unknown_choice")
        now = self.deps.clock.now_iso()
        result = await session.answer(choice_id, option_id)
        if result.status == "choice_recorded":
            self.trace.event(TraceEvent(task_id=task_id, call_id=session.call_id, event_type="user_action", speaker="user",
                            action={"name": "choice_answered", "shown_ms": int(pending.created_at.timestamp()*1000), "acted_ms": _unix_ms(now)}))
        return result

    async def join(self, task_id: str) -> JoinCallOut:
        """未知 task → ToolFailure("unknown_task")（A-18）；没有通话或未到 handoff_ready → "handoff_not_ready"。"""
        session = self._require_session(task_id, "handoff_not_ready")
        return await session.join()

    def listen(self, task_id: str) -> ListenTicketOut:
        """签发只读旁听票据；未知任务或不可旁听时沿用 ToolFailure。"""
        session = self._require_session(task_id, "listen_unavailable")
        fn = getattr(session, "listen", None)
        if fn is None:
            raise ToolFailure("listen_unavailable")
        out = fn()
        return out.model_copy(update={"task_id": task_id})

    async def return_(self, task_id: str, handoff_id: str) -> ReturnOut:
        """AC-78：交给 session.return_to_agent，状态变 resuming。
        未知 task → ToolFailure("unknown_task")（A-18）；没有进行中的转接 → "no_active_handoff"。"""
        session = self._require_session(task_id, "no_active_handoff")
        return await session.return_to_agent(handoff_id)

    async def handoff_connected(self, call_id: str) -> None:
        """D1's valid hello + sent ready callback; idempotent and durable across reconnects."""
        self.store.mark_handoff_connected(call_id, self.deps.clock.now_iso())

    async def report_result(self, task_id, call_id, outcome, confirmation_number=None):
        # Shares the start lock; the Store transaction also checks latest under a DB write lock.
        async with self._locks.setdefault(task_id, asyncio.Lock()):
            self._task(task_id)
            return await self.results.report(task_id, call_id, outcome, confirmation_number)

    async def hangup(self, task_id: str) -> HangupCallOut:
        self._task(task_id)
        async with self._hangup_locks.setdefault(task_id, asyncio.Lock()):
            session = next((s for cid, s in self._sessions.items()
                            if cid in self._remote_hangups and s.task_id == task_id), None)
            session = session or self._latest_session(task_id)
            if session is None:
                rows = self.store.list_calls(task_id)
                return HangupCallOut(status="already_ended" if rows else "no_active_call",
                    call_id=rows[-1]["call_id"] if rows else None, remote_hangup_pending=False)
            was_ended = session.ended
            pending = False
            retry = self._remote_hangups.get(session.call_id)
            try:
                if retry is not None:
                    # Preserve the carrier and SID that actually failed, never the next call's SID.
                    carrier, sid = retry
                    if getattr(getattr(carrier, "http", None), "is_closed", False) is True:
                        from waitless.call.live import CarrierClient
                        client = CarrierClient(base_url=self.settings.carrier_url)
                        try:
                            await client.hangup(sid)
                        finally:
                            await client.aclose()
                    else:
                        await carrier.hangup(sid)
                    self._remote_hangups.pop(session.call_id, None)
                else:
                    await session.hangup("user_hangup")
            except Exception:
                if not session.ended:
                    raise
                carrier = getattr(session, "carrier", None)
                sid = getattr(session, "_call_sid", None)
                if carrier is None or sid is None:
                    raise
                self._remote_hangups[session.call_id] = (carrier, sid)
                pending = True
            return HangupCallOut(status="already_ended" if was_ended else "hung_up",
                                 call_id=session.call_id, remote_hangup_pending=pending)

    def get_session(self, call_id: str) -> CallSession | None:
        return self._sessions.get(call_id)

    async def on_ended(self, call_id: str) -> None:
        """CallDeps.on_ended 指向这里：await self.results.finalize(call_id) 得到 CallResult（A-24、A-G-8），
        按 CallResult.outcome 更新 calls 行与 tasks.status。"""
        row = next((r for r in self.store.list_calls(self._sessions[call_id].task_id) if r["call_id"] == call_id), None)
        callback_at = self.deps.clock.now_iso()
        callback_ms = self.deps.clock.monotonic_ms()
        try:
            result = await self.results.finalize(call_id)
        except Exception:
            current = next((r for r in self.store.list_calls(row["task_id"]) if r["call_id"] == call_id), None) if row else None
            if current and current["result_json"]:
                saved = CallResult.model_validate_json(current["result_json"])
                if saved.result_source == "user":
                    return
            # Persist a terminal failure so polling can finish even when L4/Jev/L5 fails.
            # Retain the hangup instant (K4), excluding result-processing time.
            if row:
                events = self.trace.events(call_id=call_id)
                ended_event = next((e for e in reversed(events) if e.event_type == "call_ended"), None)
                ended_at = row["ended_at"] or callback_at
                if not row["ended_at"] and ended_event is not None and ended_event.t_ms is not None:
                    elapsed_ms = max(0, callback_ms - ended_event.t_ms)
                    ended_at = (datetime.fromisoformat(callback_at) - timedelta(milliseconds=elapsed_ms)).isoformat()
                duration = max(0, (datetime.fromisoformat(ended_at) - datetime.fromisoformat(row["started_at"])).total_seconds())
                failed = CallResult(call_id=call_id, outcome="failed", confirmation_number=None,
                                    verified=[], follow_ups=[], summary="The call result could not be completed.")
                self.store.update_call(call_id, outcome="failed", confirmation_number=None, ended_at=ended_at,
                                       duration_s=duration, result_json=failed.model_dump_json(),
                                       interrupts_used=sum(e.event_type in ("pending_choice_created", "handoff_ready") for e in events))
                if self.store.list_calls(row["task_id"])[-1]["call_id"] == call_id:
                    self.store.update_task(row["task_id"], status="failed", updated_at=self.deps.clock.now_iso())
            raise
        current = next((r for r in self.store.list_calls(row["task_id"]) if r["call_id"] == call_id), None) if row else None
        if current and current["result_json"]:
            result = CallResult.model_validate_json(current["result_json"])
        self.store.update_call(call_id, outcome=result.outcome, confirmation_number=result.confirmation_number)
        if row and self.store.list_calls(row["task_id"])[-1]["call_id"] == call_id:
            self.store.update_task(row["task_id"], status="completed" if result.outcome == "confirmed" else result.outcome,
                                   updated_at=self.deps.clock.now_iso())

    async def shutdown(self) -> None:
        for session in list(self._sessions.values()):
            if not session.ended:
                try:
                    await session.hangup("shutdown")
                except Exception as exc:
                    import logging
                    logging.getLogger(__name__).warning("Call shutdown failed: %s", type(exc).__name__)


    def _task(self, task_id):
        row = self.store.get_task(task_id)
        if row is None:
            raise ToolFailure("unknown_task")
        return row

    def _latest_session(self, task_id):
        return next((s for s in reversed(list(self._sessions.values())) if s.task_id == task_id), None)

    def _require_session(self, task_id, code):
        self._task(task_id)
        session = self._latest_session(task_id)
        if session is None or session.ended:
            raise ToolFailure(code)
        return session


def _unix_ms(iso):
    return int(datetime.fromisoformat(iso).timestamp()*1000)
