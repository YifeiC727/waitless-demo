"""live-sim 的一通电话：拨号、接线、管线、决策循环、转接（D 线；tech §6.3–§6.5、§8；AC-75、AC-76、AC-95）。

公开接口（§7）：LiveCall（实现 interfaces.LiveSession）；另有 TranscriptTap、CarrierClient、create_live_call。
"""

from typing import Any
import asyncio
import uuid
import json
import httpx
from waitless import models as m
from waitless.interfaces import ToolFailure
from waitless.call import pipeline
from waitless.call.line_ws import LineSerializer
from waitless.call.media_gate import MediaGate
from waitless.call.output_guard import OutputGuard
from waitless.call.turn_detector import TurnDetector
from waitless.call.injector import SonicTextInjector
from waitless.call.actions import LiveActionBackend
from waitless.call.handoff import HandoffBridge
from waitless.audio.vad import EnergyVad
from pipecat.serializers.twilio import TwilioFrameSerializer
from pipecat.pipeline.runner import PipelineRunner
from pipecat.frames.frames import LLMContextFrame
from pipecat.processors.aggregators.llm_context import LLMContext

from waitless.interfaces import CallDeps
from waitless.models import (
    AnswerChoiceOut,
    CallStatusOut,
    CallSpec,
    CarrierDialIn,
    CarrierDialOut,
    CarrierHangupOut,
    JoinCallOut,
    ProviderTextEvent,
    ReturnOut,
    ScenarioSelection,
)

__all__ = ["CarrierClient", "LiveCall", "TranscriptTap", "create_live_call"]


class TranscriptTap:
    """截取 USER FINAL 与 ASSISTANT FINAL，交给 TurnDetector、LiveActionBackend 与 trace（§6.5）。

    歧义表 A-12（定稿 provider_event_listener）：类名不变，但不再是管线里的一格——它是 SonicTap 唯一的订阅者
    （sonic.add_text_listener(tap.on_provider_text)）。转接期间一律丢弃、不写 trace（AC-75），这一判断只在这里做。
    """

    def __init__(self, *, deps: CallDeps, spec: CallSpec, turn_detector: Any, actions: Any, media_gate: Any,
                 guard: OutputGuard | None = None) -> None:
        self.deps, self.spec, self.turn_detector, self.actions, self.media_gate = deps, spec, turn_detector, actions, media_gate
        self.guard = guard

    async def on_provider_text(self, ev: ProviderTextEvent) -> None:
        """SonicTap 回调：不得阻塞，也不得等后续 Sonic 事件（缺口 D-G-8）；要等的工作交给后台任务。"""
        # todo(D): media_gate.in_handoff → 丢弃。USER FINAL → turn_detector.on_provider_text；
        #          ASSISTANT FINAL → waitless_utterance 事件 + actions.on_assistant_final（它自己把 DP-C2 放进后台任务）。
        #          SPECULATIVE 不写 trace。
        if self.media_gate.in_handoff or ev.stage != "FINAL":
            return
        try:
            control = json.loads(ev.text.strip())
        except (ValueError, TypeError):
            control = None
        if isinstance(control, dict) and set(control) == {"interrupted"} and control["interrupted"] is True:
            return
        if ev.role == "USER":
            await self.turn_detector.on_provider_text(ev)
        elif ev.role == "ASSISTANT":
            if self.guard is not None and ev.response is not None:
                mode = self.guard._response_mode(ev.response)
                if mode == "blocked":
                    return
                if mode != "allowed":
                    self.guard._defer_final(ev.response, lambda: self._record_final(ev))
                    return
            await self._record_final(ev)

    async def _record_final(self, ev: ProviderTextEvent) -> None:
        # A held FINAL may resolve after a handoff begins; retain AC-75 isolation.
        if self.media_gate.in_handoff:
            return
        self.deps.trace.event(m.TraceEvent(task_id=self.spec.task_id, call_id=self.spec.call_id,
            event_type="waitless_utterance", speaker="waitless", transcript=ev.text, t_ms=ev.t_ms))
        await self.actions.on_assistant_final(ev)


class CarrierClient:
    """模拟运营商 API 的客户端（§6.3）。base_url = Settings.carrier_url；http 可注入（测试用 FakeCarrier.transport）。"""

    def __init__(self, *, base_url: str, http: Any | None = None) -> None:
        if not base_url:
            raise ValueError("carrier URL is required")
        self._owns_http = not isinstance(http, httpx.AsyncClient)
        self.http = http if not self._owns_http else httpx.AsyncClient(base_url=base_url, transport=http)

    async def dial(self, req: CarrierDialIn) -> CarrierDialOut:
        """POST /carrier/calls；404 unknown_number 抛 ToolFailure 或专门异常（D 线定）。"""
        response = await self.http.post("/carrier/calls", json=req.model_dump(mode="json"))
        return self._decode(response, CarrierDialOut)

    async def hangup(self, call_sid: str) -> CarrierHangupOut:
        response = await self.http.post(f"/carrier/calls/{call_sid}/hangup")
        return self._decode(response, CarrierHangupOut)

    async def set_scenario(self, sel: ScenarioSelection) -> ScenarioSelection:
        """PUT /sim/scenario（评测每次拨号前调用，AC-84、AC-93）。"""
        response = await self.http.put("/sim/scenario", json=sel.model_dump(mode="json"))
        return self._decode(response, ScenarioSelection)

    async def get_scenario(self, line_number: str) -> ScenarioSelection:
        response = await self.http.get("/sim/scenario", params={"line_number": line_number})
        return self._decode(response, ScenarioSelection)

    async def aclose(self) -> None:
        if self._owns_http:
            await self.http.aclose()

    @staticmethod
    def _decode(response: Any, model: Any) -> Any:
        if response.is_error:
            if response.status_code == 404:
                payload = response.json()
                code = payload.get("code", payload.get("detail", {}).get("code") if isinstance(payload.get("detail"), dict) else None)
                if code in ("unknown_number", "unknown_call", "unknown_line", "unknown_scenario"):
                    raise ToolFailure(code)
            response.raise_for_status()
        return model.model_validate(response.json())


class LiveCall:
    """一通 live 电话。转接只切换媒体门，不重拨、不新建线路，callSid 不变（AC-76）。"""

    def __init__(self, spec: CallSpec, deps: CallDeps, *, carrier: CarrierClient | None = None) -> None:
        self.spec, self.deps = spec, deps
        self._owns_carrier = carrier is None
        self.carrier = carrier or CarrierClient(base_url=deps.settings.carrier_url)
        self._scenario_id = None
        self._call_sid = None
        self._phase = "idle"
        self._started_ms = deps.clock.monotonic_ms()
        self._ended_event = asyncio.Event()
        self._notified = False
        self._actions = None
        self._task = None
        self._media_gate = None
        self._guard = None
        self._bridge = None
        self._handoff_id = None
        self._handoff_decision_id = None
        self._return_lock = asyncio.Lock()
        self._round_lock = asyncio.Lock()
        self._background = set()

    @property
    def call_id(self) -> str:
        return self.spec.call_id

    @property
    def task_id(self) -> str:
        return self.spec.task_id

    @property
    def scenario_id(self) -> str | None:
        """拨号前 GET /sim/scenario 得到的场景；GET 失败为 None（A-10）。"""
        return self._scenario_id

    async def start(self) -> None:
        # todo(D): A-10（定稿 nullable_get_before_dial）：先 carrier.get_scenario(line_number=spec.to_number)，
        #          成功记下 scenario_id，失败（含 404）记 None，不阻止拨号。
        # todo(D): token = deps.line_tokens.issue(call_id)；carrier.dial(CarrierDialIn(stream_url=settings.line_ws_url,
        #          stream_token=token, to_number=spec.to_number, ...))；phase=dialing；写 call_started。
        if self._phase != "idle":
            raise RuntimeError("call already started")
        try:
            selection = await self.carrier.get_scenario(line_number=self.spec.to_number)
            self._scenario_id = selection.scenario_id
        except (httpx.HTTPError, ToolFailure):
            self._scenario_id = None
        token = self.deps.line_tokens.issue(self.call_id)
        self._phase = "dialing"
        self._started_ms = self.deps.clock.monotonic_ms()
        try:
            dial = await self.carrier.dial(CarrierDialIn(call_id=self.call_id, task_id=self.task_id,
                to_number=self.spec.to_number, stream_url=self.deps.settings.line_ws_url, stream_token=token))
        except BaseException:
            self.deps.line_tokens.revoke(self.call_id)
            raise
        self._call_sid = dial.call_sid
        self._event("call_started")

    async def attach_line(self, websocket: Any, start: Any) -> None:
        """缺口 D-G-6（定稿 document_patch_points）——测试替换点的约定：transport 与 Sonic 服务必须在调用时经
        waitless.call.pipeline 模块上的 build_transport、build_sonic_service 构造（写成 `pipeline.build_transport(...)`，
        或本模块顶层同名导入 `from waitless.call.pipeline import build_transport, build_sonic_service` 后在调用时按名字取）；
        不要在导入时把函数存进别的别名、默认参数或闭包里，测试会替换这两个模块属性。"""
        # todo(D): 构造 LineSerializer、transport、MediaGate、TurnDetector、TappedNovaSonicService、OutputGuard、
        #          SonicTextInjector、LiveActionBackend(..., call_sid=start.start.callSid)（核对等于拨号响应的 call_sid，D-G-1）；
        #          sonic.add_text_listener(TranscriptTap.on_provider_text)；跑 PipelineTask 直到结束；结束时 deps.on_ended(call_id)。
        # todo(D): 缺口 D-G-3（定稿 livecall_writes）：TurnDetector 的 on_turn 回调由本类提供，依次做——写 company_turn
        #          （speaker=company、transcript=turn.text、action={"kind", "seq"}、t_ms=turn.t_end_ms，A-08）→
        #          output_guard.on_turn(turn)（A-15）→ 组 RoundContext（transcript 不截断，A-01；prior_choices 含任务卡决定、
        #          本通作答与 Store 偏好，A-03）→ deps.gate.decide_round → actions.execute(result)。
        #          handoff_ended 也由本类写：return_to_agent（含 endpoint 的自动接回）里 media_gate.exit_handoff() 之后写。
        # todo(D): 全部时间戳（CompanyTurn、ProviderTextEvent、OutputGuard、trace）都用 deps.clock（A-15、A-08）。
        # todo(D): TurnDetector 的 menu_patterns、digit_words 取 spec.compiled.press_patterns、spec.compiled.digit_words（E-G-4）。
        # todo(D): 不写 user_action 事件（缺口 A-G-9：只由 A 的 CallManager 写）；不依赖运营商回显 mark（缺口 B-G-10）。
        # todo(D): websocket 已由 line_endpoint accept（缺口 E-G-6），这里不再 accept。
        if start.start.callSid != self._call_sid or start.start.customParameters.call_id != self.call_id or start.start.customParameters.task_id != self.task_id:
            raise ValueError("line start does not match dialed call")
        if self._task is not None:
            raise ValueError("line is already attached")
        settings, engine = self.deps.settings, self.deps.settings.engine
        serializer = LineSerializer(stream_sid=start.streamSid, params=TwilioFrameSerializer.InputParams(
            sample_rate=engine.audio.pipeline_in_sample_rate, auto_hang_up=False))
        transport = pipeline.build_transport(websocket, serializer, settings)
        sonic = pipeline.build_sonic_service(self.spec.compiled, settings)
        sonic._waitless_clock = self.deps.clock
        self._media_gate = MediaGate()
        self._guard = OutputGuard(stall_phrases=self.spec.compiled.stall_phrases,
            held_response_max_ms=engine.guard.held_response_max_ms, trace=self.deps.trace,
            call_id=self.call_id, task_id=self.task_id, clock=self.deps.clock)
        detector = TurnDetector(call_id=self.call_id,
            vad=EnergyVad(threshold_dbfs=engine.vad.threshold_dbfs, frame_ms=engine.vad.frame_ms,
                sample_rate=engine.audio.pipeline_in_sample_rate), turns=engine.turns,
            menu_patterns=self.spec.compiled.press_patterns, digit_words=self.spec.compiled.digit_words,
            on_turn=self._on_turn, clock=self.deps.clock)
        self._injector = SonicTextInjector(sonic, trace=self.deps.trace, call_id=self.call_id,
            task_id=self.task_id, clock=self.deps.clock)
        parts = pipeline.LivePipelineParts(self._media_gate.inbound, detector, sonic, self._guard, self._media_gate.outbound)
        _, self._task = pipeline.build_pipeline(self.spec.compiled, transport, settings=settings, parts=parts)
        action_deps = CallDeps(settings=self.deps.settings, store=self.deps.store, trace=self.deps.trace,
            gate=self.deps.gate, pending=self.deps.pending, llm=self.deps.llm, clock=self.deps.clock,
            line_tokens=self.deps.line_tokens, handoff_tokens=self.deps.handoff_tokens, on_ended=self._finish)
        self._actions = LiveActionBackend(spec=self.spec, deps=action_deps, injector=self._injector,
            output_guard=self._guard, media_gate=self._media_gate, pipeline_task=self._task,
            carrier=self.carrier, call_sid=self._call_sid)
        sonic.add_text_listener(TranscriptTap(deps=self.deps, spec=self.spec, turn_detector=detector,
            actions=self._actions, media_gate=self._media_gate, guard=self._guard).on_provider_text)
        add_handler = getattr(transport, "add_event_handler", None)
        if add_handler is not None:
            add_handler("on_client_disconnected", self._on_line_disconnected)
        await self._task.queue_frame(LLMContextFrame(LLMContext(messages=[])))
        flush = asyncio.create_task(self._flush_injections())
        try:
            await PipelineRunner(handle_sigint=False).run(self._task)
        finally:
            flush.cancel()
            await asyncio.gather(flush, return_exceptions=True)
            await self._finish(self.call_id)

    def status(self, debug: bool = False) -> CallStatusOut:
        # todo(D): phase 按 models.CALL_PHASE_FROM_JEV 的规则（缺口 D-G-11）：拨号后到第一轮前 dialing，之后按最近一轮
        #          DecisionResult.call_phase 映射，awaiting_user、handoff_ready、in_handoff、resuming 优先，挂断后 ended；
        #          用户接入转接期间必须是 in_handoff（handoff_endpoint 据此判断断线，D-G-5）。
        # todo(D): 转接期间 transcript_tail=[]；debug 带 skills、last_decision、guard
        #          （last_decision 按 task_id 取本任务最近一条决策：deps.trace.events(task_id=…) 里最后一条 decision，
        #          含 DP-A1、P1–P3，A-09）。
        phase = "ended" if self.ended else self._actions.phase if self._actions else self._phase
        events = self.deps.trace.events(call_id=self.call_id)
        tail = [m.TranscriptTailItem(speaker=e.speaker, text=e.transcript, t_ms=e.t_ms)
            for e in events if e.transcript and e.speaker in ("company", "waitless")]
        handoff = None
        if self._handoff_id or phase == "handoff_ready":
            state = "active" if phase == "in_handoff" else "ready" if phase == "handoff_ready" else "ended"
            handoff = m.HandoffStatus(state=state, company_name=self.spec.company_name,
                media=m.Media(ai_media="disconnected", recording=False, transcription=False)
                if state != "ended" else m.Media(ai_media="connected", recording=False, transcription=True))
        info = None
        if debug:
            decisions = [e for e in self.deps.trace.events(task_id=self.task_id) if e.event_type == "decision"]
            last = None
            if decisions:
                event = decisions[-1]
                policy = event.policy_decision
                last = m.LastDecision(decision_id=event.decision_id, point=event.decision_point,
                    intent=policy["intent"], action=policy["action"], reason_code=policy["reason_code"],
                    jev=event.jev_response["answers"], latency_ms=event.latency_ms)
            info = m.CallStatusDebug(mode=self.spec.mode,
                skills=[m.SkillCommit(name=s.name, commit=s.commit) for s in self.spec.compiled.skill_versions],
                last_decision=last, guard=self._guard.counters if self._guard else m.GuardCounters(blocked=0, released=0))
        return CallStatusOut(task_id=self.task_id, call_id=self.call_id, phase=phase, status_line=phase,
            transcript_tail=[] if phase == "in_handoff" else tail[-8:],
            pending_choice=self.deps.pending.current(self.call_id), handoff=handoff,
            interrupts_used=self._actions.interrupts_used if self._actions else 0,
            elapsed_s=max(0, self.deps.clock.monotonic_ms() - self._started_ms) / 1000,
            ended=self.ended, debug=info)

    async def answer(self, choice_id: str, option_id: str) -> AnswerChoiceOut:
        choice = self.deps.pending.current(self.call_id)
        if choice is None or choice.choice_id != choice_id:
            raise ToolFailure("unknown_choice")
        answered = self.deps.pending.answer(choice_id, option_id)
        if answered.status == "expired":
            return AnswerChoiceOut(task_id=self.task_id, choice_id=choice_id, status="expired", option_id=option_id)
        option = next(o for o in choice.options if o.id == option_id)
        decision_id = self.deps.pending.decision_of(choice_id)
        if not self.deps.trace.has_decision(decision_id):
            raise ToolFailure("unknown_choice")
        self._actions.prior_answers.append(m.PriorChoice(question_key=choice_id, choice=option_id, source="user_answer"))
        await self._actions._inject(self._actions._template("pending.answer", answer=option.label,
            option_label=option.label, option_detail=option.detail or ""),
            decision_id, template="pending.answer", check=True)
        self._actions.phase = self._actions._pending_phase
        return AnswerChoiceOut(task_id=self.task_id, choice_id=choice_id, status="choice_recorded", option_id=option_id)

    async def join(self) -> JoinCallOut:
        if self.status().phase != "handoff_ready":
            raise ToolFailure("handoff_not_ready")
        if self._handoff_id is None:
            self._handoff_id = uuid.uuid4().hex
            self._handoff_decision_id = self._actions.last_result.decision_id
        token, expiry = self.deps.handoff_tokens.issue(self.call_id, self._handoff_id)
        return JoinCallOut(task_id=self.task_id, call_id=self.call_id, handoff_id=self._handoff_id,
            ws_path=f"/ws/handoff/{self._handoff_id}", token=token, expires_at=expiry,
            company_name=self.spec.company_name,
            media=m.JoinMedia(ai_media="disconnected", recording=False, transcription=False))

    async def return_to_agent(self, handoff_id: str) -> ReturnOut:
        """media_gate.exit_handoff() → 写 handoff_ended（D-G-3；action={"dropped_sonic_outputs": output_guard.dropped_in_handoff}，
        D-G-4b）→ 注入 "handoff.resume" → phase=resuming（AC-78）。
        没有进行中的转接时抛 ToolFailure("no_active_handoff")（endpoint 自动接回时吞掉它，D-G-5）。"""
        async with self._return_lock:
            if self.ended or handoff_id != self._handoff_id or not self._media_gate or not self._media_gate.in_handoff:
                raise ToolFailure("no_active_handoff")
            await self._media_gate.exit_handoff()
            self._event("handoff_ended", self._handoff_decision_id,
                {"dropped_sonic_outputs": self._guard.dropped_in_handoff})
            self._actions.phase = "resuming"
            if self._bridge:
                await self._bridge.close("returned")
            await self._actions._inject(self._actions._template("handoff.resume"), self._handoff_decision_id,
                template="handoff.resume")
            self._event("resume_injected", self._handoff_decision_id)
            self._handoff_id = None
            return ReturnOut(task_id=self.task_id, call_id=self.call_id, status="resuming",
                media=m.ReturnMedia(ai_media="connected", recording=False, transcription=True))

    async def attach_handoff(self, websocket: Any, handoff_id: str) -> None:
        """websocket 已由 handoff_endpoint accept 并核对令牌（E-G-6）；连接关闭时返回，断开后的自动接回由 endpoint 计时（E-G-5）。"""
        if self.status().phase != "handoff_ready" or handoff_id != self._handoff_id:
            raise ToolFailure("no_active_handoff")
        self._bridge = HandoffBridge(websocket=websocket, media_gate=self._media_gate, company_name=self.spec.company_name)
        self._media_gate.attach_bridge(self._bridge)
        self._actions.phase = "in_handoff"
        reason = await self._bridge.run()
        if reason == "returned" and self._media_gate.in_handoff:
            await self.return_to_agent(handoff_id)

    async def hangup(self, reason: str) -> None:
        if not self.ended:
            try:
                if self._call_sid:
                    await self.carrier.hangup(self._call_sid)
            finally:
                await self._finish(self.call_id)

    async def _on_line_disconnected(self, transport: Any, websocket: Any) -> None:
        # Never wait for cancellation inside the transport receive callback: the
        # pipeline needs that receive task to return while processing CancelFrame.
        if not self.ended:
            task = asyncio.create_task(self._finish(self.call_id))
            self._background.add(task)
            task.add_done_callback(self._background.discard)

    @property
    def ended(self) -> bool:
        return self._ended_event.is_set()

    async def wait_ended(self) -> None:
        await self._ended_event.wait()

    def _event(self, kind: str, decision_id: str | None = None, action: dict | None = None) -> None:
        self.deps.trace.event(m.TraceEvent(task_id=self.task_id, call_id=self.call_id,
            event_type=kind, decision_id=decision_id, t_ms=self.deps.clock.monotonic_ms(), action=action))

    async def _on_turn(self, turn: m.CompanyTurn) -> None:
        if self.ended or self._media_gate.in_handoff:
            return
        self.deps.trace.event(m.TraceEvent(task_id=self.task_id, call_id=self.call_id,
            event_type="company_turn", speaker="company", transcript=turn.text, t_ms=turn.t_end_ms,
            action={"kind": turn.kind, "seq": turn.seq}))
        self._guard.on_turn(turn)
        task = asyncio.create_task(self._decide_turn(turn))
        self._background.add(task)
        task.add_done_callback(self._background.discard)

    async def _decide_turn(self, turn: m.CompanyTurn) -> None:
        async with self._round_lock:
            if self.ended or self._media_gate.in_handoff:
                return
            transcript = [m.TranscriptLine(speaker=e.speaker, text=e.transcript)
                for e in self.deps.trace.events(call_id=self.call_id) if e.transcript and e.speaker]
            ctx = m.RoundContext(call_id=self.call_id, task_id=self.task_id, compiled=self.spec.compiled,
                context=self._actions._context(), transcript=transcript)
            result = await self.deps.gate.decide_round(turn, ctx)
            await self._actions.execute(result)

    async def _flush_injections(self) -> None:
        while not self.ended:
            await self._injector.flush()
            await self.deps.clock.sleep(self.deps.settings.engine.vad.frame_ms / 1000)

    async def _finish(self, call_id: str) -> None:
        if self._notified:
            return
        self._notified = True
        self._ended_event.set()
        self._phase = "ended"
        self.deps.line_tokens.revoke(self.call_id)
        if self._bridge:
            await self._bridge.close("company_hangup")
        for task in self._background | (self._actions._tasks if self._actions else set()):
            if task is not asyncio.current_task():
                task.cancel()
        if self._task:
            await self._task.cancel()
        await self.deps.on_ended(call_id)
        if self._owns_carrier:
            await self.carrier.aclose()


def create_live_call(spec: CallSpec, deps: CallDeps) -> LiveCall:
    """interfaces.CallSessionFactory（live-sim）。"""
    return LiveCall(spec, deps)
