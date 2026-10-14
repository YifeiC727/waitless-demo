"""live 通话的动作执行器（D 线；tech §6.5 动作表；AC-66、AC-70、AC-72、AC-77；K1）。

公开接口（§7）：LiveActionBackend.execute(result)、UnknownDecision（定义在 interfaces，这里再导出；
E 线的 TextActionBackend 做同样的检查）。
"""

from typing import Any
import asyncio
import uuid
from collections import deque
from dataclasses import dataclass
from datetime import datetime
from pipecat.frames.frames import OutputDTMFUrgentFrame, OutputAudioRawFrame
from waitless.audio.codec import silence_pcm16
from waitless import models as m

from waitless.interfaces import CallDeps, UnknownDecision
from waitless.models import ActionRecord, CallSpec, DecisionResult, ProviderTextEvent

__all__ = ["LiveActionBackend", "UnknownDecision"]


@dataclass(eq=False)
class _SaidCheck:
    text: str
    attempt: int
    response: int = 0
    sent: bool = False


class LiveActionBackend:
    """实现 interfaces.ActionBackend。

    缺口 D-G-3（定稿 livecall_writes）：本类写自己动作产生的事件——dtmf_sent、injection_sent（经 injector）、
    pending_choice_created、handoff_announced、handoff_ready、handoff_started（在 media_gate.enter_handoff() 之后，
    带这次 handoff 决策的 decision_id）、hangup、say_mismatch。company_turn 与 handoff_ended 由 LiveCall 写；
    MediaGate、TurnDetector 不写 trace。
    """

    def __init__(
        self,
        *,
        spec: CallSpec,
        deps: CallDeps,
        injector: Any,
        output_guard: Any,
        media_gate: Any,
        pipeline_task: Any,
        carrier: Any,
        call_sid: str,
    ) -> None:
        """缺口 D-G-1（定稿 call_sid_kwarg）：call_sid 是本通在运营商那边的 id，LiveCall 在 attach_line 读完 start 后
        建本类时传入（核对它等于拨号响应 CarrierDialOut.call_sid）；end 动作用它调 carrier.hangup(call_sid)。"""
        self.spec, self.deps = spec, deps
        self.injector, self.output_guard, self.media_gate = injector, output_guard, media_gate
        self.pipeline_task, self.carrier, self.call_sid = pipeline_task, carrier, call_sid
        self.phase = "dialing"
        self._pending_phase = self.phase
        self.last_result = None
        self.interrupts_used = 0
        self.prior_answers = []
        self._executed = {}
        self._checks = deque()
        self._final_waiters = deque()
        self._tasks = set()

    async def execute(self, result: DecisionResult) -> ActionRecord:
        """先 deps.trace.has_decision(result.decision_id)，否则抛 UnknownDecision（AC-70）。"""
        # todo(D): press    推一次 OutputDTMFUrgentFrame.from_string(digits)，随后补 engine.dtmf.tail_silence_ms 的
        #                   静音把重采样尾部推出去（S0 V4）；写 dtmf_sent（AC-66：每个决策只推一次）。
        # todo(D): say      injector.inject(say_text, decision_id)；随后等下一段 ASSISTANT FINAL 做 DP-C2（见 on_assistant_final）。
        # todo(D): release  output_guard.release(result.turn_seq)（缺口 D-G-2：turn_seq 由 G 的 gate 填在 call_round 结果里）。
        # todo(D): ask_user L3 extract_offer_options（失败用通用 accept/decline/decide_later）→ deps.pending.create(...)
        #                   → 注入 say_templates["handoff.ask_user"]；写 pending_choice_created；用户作答或超时后注入
        #                   "pending.answer" 或 "pending.safe_default"（沿用同一 decision_id）。
        # todo(D): handoff  缺口 D-G-4（定稿 ready_before_started，Jev .58）的固定顺序：注入 "handoff.announce" →
        #                   写 handoff_announced → 等 Sonic 说完（ASSISTANT FINAL 到达，且 OutputGuard 放行最后一帧后
        #                   满 engine.handoff.announce_idle_ms，AC-77）→ 写 handoff_ready、phase=handoff_ready →
        #                   media_gate.enter_handoff() → 写 handoff_started（带本次 decision_id，D-G-3）。
        #                   这样转接窗口里只有 handoff_started 与之后 LiveCall 写的 handoff_ended（AC-75）。
        # todo(D): end      注入 "end.goodbye"，等说完 → carrier.hangup(self.call_sid)（D-G-1）→ 写 hangup（带 decision_id）
        #                   → deps.on_ended(call_id)。
        # todo(D): wait     不动作。
        if not self.deps.trace.has_decision(result.decision_id):
            raise UnknownDecision(result.decision_id)
        if result.decision_id in self._executed:
            return self._executed[result.decision_id]
        started = self.deps.clock.monotonic_ms()
        record = ActionRecord(decision_id=result.decision_id, action=result.action,
            started_ms=started, finished_ms=started, ok=False, detail="in progress")
        self._executed[result.decision_id] = record
        self.last_result = result
        if result.call_phase and (self.phase not in m.ACTION_DRIVEN_PHASES
                or (self.phase == "resuming" and result.point == "call_round")):
            self.phase = m.CALL_PHASE_FROM_JEV[result.call_phase]
        if result.action == "press":
            if result.turn_seq is not None:
                await self.output_guard.discard(result.turn_seq, "press")
            self.output_guard.set_state("ivr" if result.call_phase == "ivr_menu" else "conversation")
            await self.pipeline_task.queue_frame(OutputDTMFUrgentFrame.from_string(result.digits))
            audio = silence_pcm16(self.deps.settings.engine.dtmf.tail_silence_ms,
                self.deps.settings.engine.audio.pipeline_in_sample_rate)
            if audio:
                tail = OutputAudioRawFrame(audio=audio,
                    sample_rate=self.deps.settings.engine.audio.pipeline_in_sample_rate, num_channels=1)
                tail._waitless_dtmf_tail = True
                await self.pipeline_task.queue_frame(tail)
            self._event("dtmf_sent", result.decision_id, {"digits": result.digits})
        elif result.action == "release":
            self.output_guard.set_state("conversation")
            if result.turn_seq is None:
                raise ValueError("release needs a company turn sequence")
            await self.output_guard.release(result.turn_seq)
        elif result.action == "say":
            await self._inject(result.say_text, result.decision_id, check=True)
        elif result.action == "ask_user":
            await self._ask_user(result)
        elif result.action in ("handoff", "end"):
            key = "handoff.announce" if result.action == "handoff" else "end.goodbye"
            final = asyncio.Event()
            waiter = {"final": final, "sent": False, "response": None, "audio_serial": None}
            self._final_waiters.append(waiter)
            def sending(response: int):
                # Snapshot before the send awaits; equal timestamps cannot distinguish audio frames.
                waiter["response"] = max(response, self.output_guard._response_number)
                waiter["audio_serial"] = self.output_guard._audio_serial
                return lambda: waiter.update(sent=True)
            self.injector._send_observers[result.decision_id] = sending
            self.output_guard.set_state("injected" if result.action == "handoff" else "ending")
            try:
                await self.injector.inject(self._template(key), result.decision_id, template=key)
                if result.action == "handoff":
                    self._event("handoff_announced", result.decision_id, {"text": self._template(key)})
                await final.wait()
                await self._wait_output_idle(waiter["audio_serial"])
            finally:
                self.injector._send_observers.pop(result.decision_id, None)
                if waiter in self._final_waiters:
                    self._final_waiters.remove(waiter)
            if result.action == "handoff":
                # Pending checks and choice timers must not add trace rows in the private window.
                tasks = tuple(task for task in self._tasks if task is not asyncio.current_task())
                for task in tasks:
                    task.cancel()
                await asyncio.gather(*tasks, return_exceptions=True)
                self._checks.clear()
                self.phase = "handoff_ready"
                self.interrupts_used += 1
                self._event("handoff_ready", result.decision_id)
                self.output_guard.set_state("handoff")
                await self.media_gate.enter_handoff()
                self._event("handoff_started", result.decision_id)
            else:
                await self.carrier.hangup(self.call_sid)
                self.phase = "ended"
                self._event("hangup", result.decision_id)
                await self.deps.on_ended(self.spec.call_id)
        record.finished_ms = self.deps.clock.monotonic_ms()
        record.ok, record.detail = True, ""
        return record

    async def on_assistant_final(self, ev: ProviderTextEvent) -> None:
        """注入后的下一段 ASSISTANT FINAL（SonicTap 事件，不是 TTSTextFrame）送 DP-C2 said_as_decided（AC-72）。

        歧义表 A-02（定稿 wait_except_c2）：
        - 组 DecisionRequest(point="said_as_decided", call_id, transcript=[…, 最后一行 speaker=waitless、text=ev.text],
          template_vars={models.SAID_SUMMARY_VAR: 本次注入的文字（不含前缀）}, attempt=0 或 1)，交 deps.gate.decide。
        - 按返回的 action 执行：said_ok（wait）→ 结束核对；reinject（say）→ injector.inject(say_text, 这条 DP-C2 的
          decision_id)，下一段 ASSISTANT FINAL 再核对一次（attempt=1）；safe_default → 写 say_mismatch 事件，
          再按 action 执行（建议 end：注入 end.goodbye、挂断，结果落到 needs_follow_up）。
        - 不自己比较阈值、不自己数次数之外的逻辑：判定全在 G 的策略里（D10、D11）。
        - 缺口 D-G-8（定稿 listeners_must_not_block）：本方法经 TranscriptTap 从 SonicTap 回调里调用，不得阻塞——
          DP-C2 决策与随后的重新注入用 asyncio.create_task 交给后台任务，这里立即返回。"""
        if ev.role != "ASSISTANT" or ev.stage != "FINAL":
            return
        if self._final_waiters:
            waiter = self._final_waiters[0]
            if waiter["sent"] and ev.response is not None and ev.response > waiter["response"]:
                self._final_waiters.popleft()["final"].set()
        if self._checks:
            check = self._checks[0]
            if isinstance(check, _SaidCheck):
                if ev.response is not None and (not check.sent or ev.response <= check.response):
                    return
                text, attempt = check.text, check.attempt
            else:
                # Legacy tuples and injectors without send observers retain next-FINAL behavior.
                text, attempt = check
            self._checks.popleft()
            self._spawn(self._check_said(ev, text, attempt))

    def _spawn(self, coro: Any) -> asyncio.Task:
        task = asyncio.create_task(coro)
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)
        return task

    def _event(self, kind: str, decision_id: str, action: dict | None = None) -> None:
        self.deps.trace.event(m.TraceEvent(task_id=self.spec.task_id, call_id=self.spec.call_id,
            event_type=kind, decision_id=decision_id, t_ms=self.deps.clock.monotonic_ms(), action=action))

    def _template(self, key: str, **values: str) -> str:
        variables = {**self.spec.compiled.allowed_fields, **values}
        return self.spec.compiled.say_templates[key].format(**variables)

    def _context(self) -> m.DecisionContext:
        card = self.spec.compiled.task_card
        prior = [m.PriorChoice(question_key=d.question_id, choice=d.selected, source="task_card")
            for d in card.precall_decisions if d.selected is not None]
        prior.extend(self.prior_answers)
        prior.extend(m.PriorChoice.from_preference(p) for p in self.deps.store.list_preferences(card.company.company_id))
        return m.DecisionContext(task_card=card, allowed_fields=self.spec.compiled.allowed_fields,
            standing_rules=self.deps.store.list_standing_rules(), prior_choices=prior,
            interrupts_used=self.interrupts_used, phase=self.phase)

    async def _inject(self, text: str, decision_id: str, *, check: bool = False,
                      attempt: int = 0, template: str | None = None) -> None:
        self.output_guard.set_state("injected")
        observers = getattr(self.injector, "_send_observers", None) if check else None
        pending_check = None
        if check:
            if observers is None:
                self._checks.append((text, attempt))
            else:
                pending_check = _SaidCheck(text, attempt)
                self._checks.append(pending_check)

                def sending(response: int):
                    pending_check.response = max(response, self.output_guard._response_number)

                    def sent():
                        pending_check.sent = True
                        if observers.get(decision_id) is sending:
                            observers.pop(decision_id)

                    return sent

                # inject() may return with the instruction queued during continuation.
                # Keep this observer until flush() actually sends it.
                observers[decision_id] = sending
        try:
            await self.injector.inject(text, decision_id, template=template)
        except BaseException:
            if pending_check is not None:
                if observers.get(decision_id) is sending:
                    observers.pop(decision_id)
                if pending_check in self._checks:
                    self._checks.remove(pending_check)
            raise

    async def _check_said(self, ev: ProviderTextEvent, text: str, attempt: int) -> None:
        transcript = [m.TranscriptLine(speaker=e.speaker, text=e.transcript)
            for e in self.deps.trace.events(call_id=self.spec.call_id) if e.transcript and e.speaker]
        if not transcript or transcript[-1].text != ev.text:
            transcript.append(m.TranscriptLine(speaker="waitless", text=ev.text))
        request = m.DecisionRequest(decision_id=uuid.uuid4().hex, point="said_as_decided",
            call_id=self.spec.call_id, task_id=self.spec.task_id, turn=None, candidates=[],
            context=self._context(), transcript=transcript, template_vars={m.SAID_SUMMARY_VAR: text}, attempt=attempt)
        result = await self.deps.gate.decide("said_as_decided", request)
        if not self.deps.trace.has_decision(result.decision_id):
            raise UnknownDecision(result.decision_id)
        if result.intent == m.SAID_REINJECT and result.action == "say":
            await self._inject(result.say_text, result.decision_id, check=True, attempt=attempt + 1)
        elif result.reason_code == m.SAY_MISMATCH or result.intent == m.SAID_SAFE_DEFAULT:
            self._event("say_mismatch", result.decision_id)
            await self.execute(result)
        elif result.action != "wait":
            await self.execute(result)

    async def _wait_output_idle(self, audio_serial: int) -> None:
        idle = self.deps.settings.engine.handoff.announce_idle_ms
        while True:
            if self.output_guard._audio_serial > audio_serial:
                elapsed = self.deps.clock.monotonic_ms() - self.output_guard._last_audio_ms
                if elapsed >= idle:
                    return
                delay = min(idle - elapsed, self.deps.settings.engine.vad.frame_ms)
            else:
                delay = self.deps.settings.engine.vad.frame_ms
            await self.deps.clock.sleep(delay / 1000)

    async def _ask_user(self, result: DecisionResult) -> None:
        transcript = [e.transcript for e in self.deps.trace.events(call_id=self.spec.call_id)
            if e.event_type == "company_turn" and e.transcript]
        question = transcript[-1] if transcript else "Please choose how to proceed."
        try:
            extracted = await self.deps.llm.extract_offer_options(question)
            options = [m.Option(id=str(i + 1), label=o.label, detail=o.detail) for i, o in enumerate(extracted)]
        except Exception:
            options = []
        if not options:
            options = [m.Option(id=k, label=k.replace("_", " "), detail="")
                for k in ("accept", "decline", "decide_later")]
        policy = self.spec.compiled.policy.get("global", {}).get("ask_user", {})
        choice = self.deps.pending.create(call_id=self.spec.call_id, decision_id=result.decision_id,
            question_text=question, options=options[:policy["max_options"]], timeout_s=policy["timeout_sec"])
        self._pending_phase = self.phase
        self.phase = "awaiting_user"
        self.interrupts_used += 1
        self._event("pending_choice_created", result.decision_id, {"choice_id": choice.choice_id})
        await self._inject(self._template("handoff.ask_user"), result.decision_id, template="handoff.ask_user")
        self._spawn(self._expire_pending(choice, result.decision_id))

    async def _expire_pending(self, choice: m.PendingChoice, decision_id: str) -> None:
        await self.deps.clock.sleep(max(0, (choice.expires_at - datetime.fromisoformat(self.deps.clock.now_iso())).total_seconds()))
        current = self.deps.pending.get(choice.choice_id)
        if current is None or current.status != "open":
            return
        self.deps.pending.expire_due()
        await self._inject(self._template("pending.safe_default"), decision_id, template="pending.safe_default")
        self.phase = "resuming"
