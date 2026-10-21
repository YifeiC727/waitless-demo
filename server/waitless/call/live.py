"""Live call lifecycle, phone-agent tools, provider transcripts and media handoff."""

from typing import Any
from dataclasses import replace
import asyncio
import inspect
import uuid
import json
import math
from datetime import datetime, timedelta
import httpx
from waitless import models as m
from waitless.interfaces import ToolFailure
from waitless.call import pipeline
from waitless.call.line_ws import LineSerializer
from waitless.call.media_gate import MediaGate, response_identity
from waitless.call.agent_tools import AgentToolbox, OutputMonitor, AGENT_EVENT_PREFIX, load_tool_error_hints
from waitless.call.keypad import KeypadDecider, load_keypad_config, load_speaker_config, load_transfer_config
from waitless.call.injector import SonicTextInjector
from waitless.call.handoff import HandoffBridge, HandoffTokens
from waitless.call.listen import ListenHub, ListenTicketOut
from pipecat.serializers.twilio import TwilioFrameSerializer
from pipecat.pipeline.runner import PipelineRunner
from pipecat.frames.frames import LLMContextFrame
from pipecat.processors.aggregators.llm_context import LLMContext
from pipecat.processors.aggregators.llm_response_universal import LLMContextAggregatorPair

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


# Process-owned references and records survive removal from the active call set.
UNRECLAIMED: dict[asyncio.Task, tuple[str, str]] = {}
TEARDOWN_STATE: dict[str, list[dict]] = {}


class _CommitStopped(Exception):
    """An external finish has ended the call at a commit stage boundary."""


class _CommitFailed(Exception):
    """The bounded step has already recorded the underlying failure."""


class TranscriptTap:
    """Record provider FINALs, coalescing assistant blocks by response number."""

    def __init__(self, *, deps, spec, media_gate, toolbox, monitor=None, keypad=None):
        self.deps, self.spec, self.media_gate = deps, spec, media_gate
        self.toolbox, self.monitor = toolbox, monitor
        self.keypad = keypad
        self._keypad_tasks = set()
        self._closed = False
        self._seq = 0
        self._parts, self._speculative, self._ended = {}, {}, {}
        self._completed, self._timeouts = set(), {}
        self._unspoken = {}
        observers = getattr(media_gate, 'output_observers', None)
        if observers is not None:
            observers.append(self._on_audible)
        if monitor is not None:
            monitor.response_observers.append(self.on_response_end)
            monitor.handoff_observers.append(self.discard_pending)

    def discard_pending(self):
        self._completed.update(self._parts)
        self._completed.update(self._speculative)
        self._unspoken.clear()
        self._parts.clear()
        self._speculative.clear()
        self._ended.clear()
        for task in self._timeouts.values():
            task.cancel()
        self._timeouts.clear()

    async def on_provider_text(self, ev):
        if self._closed or self.media_gate.in_handoff or getattr(self.media_gate, 'output_state', None) == 'ended':
            self.discard_pending()
            if ev.role == 'ASSISTANT' and ev.response is not None:
                self._completed.add(ev.response)
            return
        # Provider protocol control records are not spoken text.
        try:
            control = json.loads(ev.text.strip())
        except (ValueError, TypeError):
            control = None
        interrupted = isinstance(control, dict) and set(control) == {'interrupted'} and control['interrupted'] is True
        number = ev.response
        if ev.role == 'ASSISTANT' and number in self._completed:
            return
        if ev.stage == 'SPECULATIVE':
            if ev.role == 'ASSISTANT' and number is not None and not interrupted:
                self._speculative[number] = self._speculative.get(number, 0) + 1
            return
        if ev.stage != 'FINAL':
            return
        if interrupted:
            if ev.role == 'ASSISTANT' and number is not None:
                await self._complete(number)
                self._completed.add(number)
            return
        if ev.role == 'USER':
            if ev.text.lstrip().startswith(AGENT_EVENT_PREFIX.rstrip()):
                return
            self._seq += 1
            self.deps.trace.event(m.TraceEvent(task_id=self.spec.task_id, call_id=self.spec.call_id,
                event_type='company_turn', speaker='company', transcript=ev.text, t_ms=ev.t_ms,
                action={'kind': 'menu' if self.toolbox.phase == 'ivr' else 'speech', 'seq': self._seq}))
            if ev.text.strip():
                self.toolbox.note_speech()
            if self.keypad is not None:
                self.discard_pending()
                self.media_gate.begin_pending(self._seq)
                self.keypad.begin_turn(self._seq)
                await self.media_gate.clear_output()
                task = asyncio.create_task(self.keypad.on_company_turn(self._seq))
                self._keypad_tasks.add(task)
                task.add_done_callback(self._keypad_done)
        elif ev.role == 'ASSISTANT':
            if number is None:
                self._write(ev)
                return
            for older in sorted(n for n in self._parts if n < number):
                await self._complete(older)
            self._parts.setdefault(number, []).append(ev)
            if ev.stop_reason == 'INTERRUPTED':
                await self._complete(number)
            elif number in self._ended:
                await self._maybe_complete(number)
            elif self.monitor is None and ev.stop_reason == 'END_TURN':
                await self._complete(number)

    async def on_response_end(self, number, t_ms):
        if self.media_gate.in_handoff or getattr(self.media_gate, 'output_state', None) == 'ended':
            self.discard_pending()
            self._completed.add(number)
            return
        self._ended[number] = t_ms
        await self._maybe_complete(number)

    async def _maybe_complete(self, number):
        parts = self._parts.get(number)
        if not parts or number in self._completed:
            return
        expected = self._speculative.get(number, 0)
        if len(parts) >= max(1, expected):
            await self._complete(number)
        elif number not in self._timeouts:
            task = asyncio.create_task(self._timeout(number))
            self._timeouts[number] = task

    async def _timeout(self, number):
        deadline = self._ended[number] + self.deps.settings.engine.guard.held_response_max_ms
        await self.deps.clock.sleep(max(0, deadline - self.deps.clock.monotonic_ms()) / 1000)
        if self.media_gate.in_handoff or getattr(self.media_gate, 'output_state', None) == 'ended':
            self.discard_pending()
        else:
            await self._complete(number)

    async def _complete(self, number):
        parts = self._parts.pop(number, [])
        if not parts:
            return
        self._completed.add(number)
        self._speculative.pop(number, None)
        self._ended.pop(number, None)
        task = self._timeouts.pop(number, None)
        if task is not None and task is not asyncio.current_task():
            task.cancel()
        if not self.media_gate.in_handoff and getattr(self.media_gate, 'output_state', None) != 'ended':
            self._write(parts[-1].model_copy(update={'text': ''.join(p.text for p in parts)}))

    async def _on_audible(self, identity):
        ev = self._unspoken.pop(identity, None)
        if ev is not None and not self.media_gate.in_handoff:
            self._write(ev)

    def _write(self, ev):
        identity = getattr(ev, 'response_identity', None)
        if identity is not None and not self.media_gate.response_audible(identity):
            self._unspoken[identity] = ev
            return
        if identity is None and self.keypad is not None:
            return
        self.deps.trace.event(m.TraceEvent(task_id=self.spec.task_id, call_id=self.spec.call_id,
            event_type='waitless_utterance', speaker='waitless', transcript=ev.text, t_ms=ev.t_ms,
            action={'response_identity': response_identity(identity)}))
        if ev.text.strip() and not ev.text.lstrip().startswith(AGENT_EVENT_PREFIX.rstrip()):
            self.toolbox.note_speech()

    def _keypad_done(self, task):
        self._keypad_tasks.discard(task)
        if not task.cancelled():
            task.exception()  # Retrieve background errors; never block provider transcripts.

    async def close(self, exclude: frozenset[asyncio.Task] = frozenset()):
        self._closed = True
        tasks = tuple(self._keypad_tasks - exclude - {asyncio.current_task()})
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        for number in tuple(self._parts):
            await self._complete(number)
        self.discard_pending()


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
        self._finish_reason = None
        self._finish_task = None
        self._finish_waiters = set()
        self._line_disconnected = False
        self._toolbox = None
        self._tap = None
        self._keypad = None
        self._monitor = None
        self._task = None
        self._media_gate = None
        self._bridge = None
        self._listen_tokens = HandoffTokens(deps.clock, ttl_s=deps.settings.engine.handoff.token_ttl_s)
        self._listen_hub = None
        self._handoff_id = None
        self._handoff_decision_id = None
        self._return_lock = asyncio.Lock()
        self._background = set()
        self._handoff_lock = asyncio.Lock()
        self._hangup_lock = asyncio.Lock()
        self._handoff_generation = 0
        self._handoff_deadline_ms = None
        self._handoff_expires_at = None
        self._handoff_timeout_reason = None
        self._handoff_timer = None
        self._handoff_ever_joined = False
        self._remote_hangup_pending = False
        self._system_handoff = False
        self._handoff_kind = None
        self._handoff_commit = None
        self._failure_hangup = None
        self._handoff_confirmations = set()
        self._handoff_confirmation_failed = False

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
        if start.start.callSid != self._call_sid or start.start.customParameters.call_id != self.call_id or start.start.customParameters.task_id != self.task_id:
            raise ValueError("line start does not match dialed call")
        if self._task is not None:
            raise ValueError("line is already attached")
        settings, engine = self.deps.settings, self.deps.settings.engine
        try:
            speaker_config = load_speaker_config(settings.skills_dir)
            transfer_config = load_transfer_config(settings.skills_dir)
        except Exception:
            await self.hangup("configuration_failed")
            raise
        serializer = LineSerializer(stream_sid=start.streamSid, params=TwilioFrameSerializer.InputParams(
            sample_rate=engine.audio.pipeline_in_sample_rate, auto_hang_up=False))
        transport = pipeline.build_transport(websocket, serializer, settings)
        self._media_gate = MediaGate(dtmf=engine.dtmf, clock=self.deps.clock,
            output_buffer_ms=engine.speaker.output_buffer_ms, fact_sink=self._gate_fact)
        self._listen_hub = ListenHub(media_gate=self._media_gate,
                                     sample_rate=engine.audio.pipeline_in_sample_rate)
        serializer.listen_hub = self._listen_hub
        self._monitor = OutputMonitor(clock=self.deps.clock, media_gate=self._media_gate)
        tool_deps = replace(self.deps, on_ended=self._finish)
        self._toolbox = AgentToolbox(spec=self.spec, deps=tool_deps, pipeline_task=None,
            media_gate=self._media_gate, monitor=self._monitor, injector=None,
            carrier=self.carrier, call_sid=self._call_sid,
            error_hints=load_tool_error_hints(settings.skills_dir))
        tools = []
        self._context = LLMContext(messages=[], tools=tools)
        ctx = LLMContextAggregatorPair(self._context)
        sonic = pipeline.build_sonic_service(self.spec.compiled, settings, tools=tools)
        sonic._waitless_clock = self.deps.clock
        self._toolbox._sonic = sonic
        bind = getattr(sonic, 'bind_media_gate', None)
        if bind is not None:
            bind(self._media_gate)
        self._injector = SonicTextInjector(sonic, trace=self.deps.trace, call_id=self.call_id,
            task_id=self.task_id, clock=self.deps.clock)
        parts = pipeline.LivePipelineParts(self._media_gate.inbound, ctx, sonic, self._monitor, self._media_gate.outbound)
        _, self._task = pipeline.build_pipeline(self.spec.compiled, transport, settings=settings, parts=parts)
        self._toolbox.pipeline_task, self._toolbox.injector = self._task, self._injector
        self._keypad = KeypadDecider(spec=self.spec, deps=tool_deps, pipeline_task=lambda: self._task,
            media_gate=self._media_gate, injector=self._injector, config=load_keypad_config(settings.skills_dir),
            speaker_config=speaker_config, transfer_config=transfer_config, on_speaker=self._on_speaker)
        self._tap = TranscriptTap(deps=self.deps, spec=self.spec, media_gate=self._media_gate,
            toolbox=self._toolbox, monitor=self._monitor, keypad=self._keypad)
        sonic.add_text_listener(self._tap.on_provider_text)
        add_handler = getattr(transport, "add_event_handler", None)
        if add_handler is not None:
            add_handler("on_client_disconnected", self._on_line_disconnected)
        await self._task.queue_frame(LLMContextFrame(self._context))
        flush = asyncio.create_task(self._flush_injections())
        try:
            await PipelineRunner(handle_sigint=False).run(self._task)
        finally:
            flush.cancel()
            await asyncio.gather(flush, return_exceptions=True)
            await self._finish(self.call_id)

    def status(self, debug: bool = False) -> CallStatusOut:
        phase = ("ended" if self.ended else
                 (self._toolbox.action_phase or self._toolbox.phase) if self._toolbox else self._phase)
        events = self.deps.trace.events(call_id=self.call_id)
        tail = [m.TranscriptTailItem(speaker=e.speaker, text=e.transcript, t_ms=e.t_ms)
            for e in events if e.transcript and e.speaker in ("company", "waitless")]
        handoff = None
        if self._handoff_id or phase == "handoff_ready":
            state = "active" if phase == "in_handoff" else "ready" if phase == "handoff_ready" else "ended"
            kind = (self._handoff_kind or 'live_human') if self._system_handoff else None
            notification_fields = {'live_human': 'notification_text',
                'transfer_connected': 'connected_text', 'needs_user': 'needs_user_text'}
            handoff = m.HandoffStatus(state=state, company_name=self.spec.company_name,
                reason=getattr(self.deps.settings.engine.handoff, notification_fields[kind]) if kind else None,
                reason_kind=kind,
                generation=self._handoff_generation,
                expires_at=self._handoff_expires_at if state == 'ready' else None,
                remaining_s=max(0, math.ceil((self._handoff_deadline_ms - self.deps.clock.monotonic_ms()) / 1000))
                    if state == 'ready' and self._handoff_deadline_ms is not None else None,
                media=m.Media(ai_media="disconnected", recording=False, transcription=False))
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
                last_decision=last, guard=m.GuardCounters(blocked=0, released=0))
        return CallStatusOut(task_id=self.task_id, call_id=self.call_id, phase=phase,
            status_line=(self._toolbox.status_line if self._toolbox else None) or phase,
            transcript_tail=[] if phase == "in_handoff" else tail[-8:],
            pending_choice=self.deps.pending.current(self.call_id), handoff=handoff,
            interrupts_used=self._toolbox.interrupts_used if self._toolbox else 0,
            elapsed_s=max(0, self.deps.clock.monotonic_ms() - self._started_ms) / 1000,
            ended=self.ended, debug=info)

    async def answer(self, choice_id: str, option_id: str) -> AnswerChoiceOut:
        choice = self.deps.pending.current(self.call_id)
        if choice is None or choice.choice_id != choice_id:
            raise ToolFailure("unknown_choice")
        answered = self.deps.pending.answer(choice_id, option_id)
        if answered.status == "expired":
            await self._toolbox.on_choice_expired(answered)
            return AnswerChoiceOut(task_id=self.task_id, choice_id=choice_id, status="expired", option_id=option_id)
        decision_id = self.deps.pending.decision_of(choice_id)
        if not self.deps.trace.has_decision(decision_id):
            raise ToolFailure("unknown_choice")
        self._event('user_answered', decision_id, {'choice_id': choice_id, 'option_id': option_id})
        await self._toolbox.on_user_answer(answered, option_id)
        return AnswerChoiceOut(task_id=self.task_id, choice_id=choice_id, status="choice_recorded", option_id=option_id)

    def listen(self) -> ListenTicketOut:
        if self.ended or self.status().phase in ('handoff_ready', 'in_handoff'):
            raise ToolFailure('listen_unavailable')
        token, expiry = self._listen_tokens.issue(self.call_id, self.call_id)
        return ListenTicketOut(task_id=self.task_id, call_id=self.call_id,
            ws_path=f'/ws/listen/{self.call_id}', token=token, expires_at=expiry)

    def consume_listen_token(self, token: str) -> bool:
        return self._listen_tokens.consume(self.call_id, token) == self.call_id

    async def attach_listen(self, websocket: Any) -> None:
        if (self.ended or self.status().phase in ('handoff_ready', 'in_handoff')
                or self._listen_hub is None):
            await websocket.close(code=m.WS_CLOSE_UNAUTHORIZED)
            return
        await self._listen_hub.attach(websocket)

    def _handoff_expired(self):
        return self._handoff_deadline_ms is not None and self.deps.clock.monotonic_ms() >= self._handoff_deadline_ms

    def _set_handoff_deadline(self, seconds, reason):
        if self._handoff_timer is not None:
            self._handoff_timer.cancel()
        self._handoff_deadline_ms = self.deps.clock.monotonic_ms() + seconds * 1000
        self._handoff_expires_at = datetime.fromisoformat(self.deps.clock.now_iso()) + timedelta(seconds=seconds)
        self._handoff_timeout_reason = reason
        self._arm_handoff_timer()

    def _arm_handoff_timer(self):
        if self._handoff_timer is not None:
            self._handoff_timer.cancel()
        generation, deadline = self._handoff_generation, self._handoff_deadline_ms
        reason = self._handoff_timeout_reason
        async def expire():
            await self.deps.clock.sleep(max(0, deadline - self.deps.clock.monotonic_ms()) / 1000)
            if (not self.ended and generation == self._handoff_generation
                    and deadline == self._handoff_deadline_ms and self._handoff_expired()):
                await self.hangup(reason)
        self._handoff_timer = asyncio.create_task(expire())
        self._background.add(self._handoff_timer)
        self._handoff_timer.add_done_callback(self._background.discard)

    async def _on_speaker(self, seq, verdict, decision_id):
        if self.ended or self._media_gate.in_handoff or seq != self._media_gate.company_seq:
            return
        phase = verdict.phase
        if phase is not None and self._toolbox.phase != phase:
            self._toolbox.phase = phase
            self._toolbox.status_line = phase
            self._event('status_updated', decision_id, {'phase': phase, 'status_line': phase,
                'speaker': verdict.gate_speaker, 'speaker_class': verdict.speaker_class})
        if verdict.action == 'handoff':
            await self.system_handoff({'seq': seq, 'speaker': verdict.gate_speaker, 'decision_id': decision_id})
        else:
            await self._media_gate.resolve_speaker(seq, verdict.gate_speaker)

    def _record_failure(self, stage, error_type, *, after_ended=False):
        record = {'stage': stage, 'error_type': error_type,
                  't_ms': self.deps.clock.monotonic_ms()}
        if after_ended:
            record['after_ended'] = True
        TEARDOWN_STATE.setdefault(self.call_id, []).append(record)
        try:
            self._event('error', action=dict(record))
        except Exception:
            pass

    def _retain_step(self, step, name):
        UNRECLAIMED[step] = (self.call_id, name)
        def completed(task):
            try:
                if task.cancelled():
                    outcome = 'LateCancelled'
                else:
                    error = task.exception()
                    outcome = type(error).__name__ if error is not None else 'LateDone'
                self._record_failure('late:' + name, outcome)
            finally:
                UNRECLAIMED.pop(task, None)
        step.add_done_callback(completed)

    async def _bounded(self, name, coro_fn, *, context, propagate_error=False):
        async def invoke():
            result = coro_fn()
            return await result if inspect.isawaitable(result) else result
        # Python is pinned to 3.12. Start this independent step immediately:
        # synchronous revocation and already-ready closes need no scheduler hop.
        # Blocking steps still use the same two wait/cancellation budgets.
        step = asyncio.eager_task_factory(asyncio.get_running_loop(), invoke())
        policy = self.deps.settings.engine.speaker
        if step.done():
            done = {step}
        else:
            done, _ = await asyncio.wait({step}, timeout=policy.teardown_step_timeout_ms / 1000)
        failure = None
        if not done:
            step.cancel()
            done, _ = await asyncio.wait({step}, timeout=policy.teardown_cancel_grace_ms / 1000)
            failure = 'TimeoutError'
            if not done:
                # Lifetime registration must precede any ended/control-flow check.
                self._retain_step(step, name)
                failure = 'Unreclaimed'
        result = None
        business_error = None
        if done:
            try:
                result = step.result()
            except (Exception, asyncio.CancelledError) as error:
                # Budget expiry wins even if cancellation changes the exception.
                if failure is None and propagate_error and isinstance(error, Exception):
                    business_error = error
                failure = failure or type(error).__name__
        if failure is not None:
            self._record_failure(name, failure, after_ended=context == 'commit' and self.ended)
        if context == 'commit':
            if self.ended:
                raise _CommitStopped()
            if failure is not None:
                raise _CommitFailed()
        if business_error is not None:
            raise business_error
        return result

    async def system_handoff(self, trigger, kind='live_human', precheck=None):
        """Hold the lock only to validate and acquire the single commit task."""
        async with self._handoff_lock:
            commit = self._handoff_commit
            if commit is None:
                fallback = 'source_decision_id' in trigger
                if self.ended or self._system_handoff or self._media_gate.in_handoff:
                    return
                if not fallback and trigger['seq'] != self._media_gate.company_seq:
                    return
                if precheck is not None and not precheck():
                    return
                self._system_handoff = True
                self._handoff_kind = kind
                self._handoff_decision_id = uuid.uuid4().hex if fallback else trigger['decision_id']
                self._toolbox.handoff_decision_id = self._handoff_decision_id
                commit = self._handoff_commit = asyncio.create_task(self._commit_handoff(trigger))
        await asyncio.shield(commit)

    async def _commit_handoff(self, trigger):
        stage = 'handoff:decision'
        try:
            if self.ended:
                return
            if 'source_decision_id' in trigger:
                def decision():
                    policy = dict(intent='speaker_fallback', action='handoff', handoff_kind=self._handoff_kind,
                        reason_code=trigger['reason_code'], policy_rule='phone-agent/speaker-fallback',
                        source_decision_id=trigger['source_decision_id'])
                    for field in ('quiet_audio_ms', 'quiet_wall_ms'):
                        if field in trigger:
                            policy[field] = trigger[field]
                    self.deps.trace.event(m.TraceEvent(task_id=self.task_id, call_id=self.call_id,
                        event_type='decision', decision_id=self._handoff_decision_id,
                        decision_point='agent_tool', action={'turn_seq': trigger['seq']},
                        jev_request=trigger['jev_request'], jev_response=trigger['jev_response'],
                        policy_decision=policy, latency_ms=trigger.get('latency_ms', 0),
                        t_ms=self.deps.clock.monotonic_ms()))
                await self._bounded(stage, decision, context='commit')
            stage = 'handoff:enter_handoff'
            await self._bounded(stage, self._media_gate.enter_handoff, context='commit')
            if self._tap:
                stage = 'handoff:discard_pending'
                await self._bounded(stage, self._tap.discard_pending, context='commit')
            if self._listen_hub:
                stage = 'handoff:listen_hub'
                await self._bounded(stage, lambda: self._listen_hub.close('handoff'), context='commit')
            self._handoff_id = uuid.uuid4().hex
            self._handoff_generation += 1
            self._toolbox.action_phase = self._phase = 'handoff_ready'
            stage = 'handoff:announced'
            await self._bounded(stage, lambda: self._event('handoff_announced',
                self._handoff_decision_id, {'text': ''}), context='commit')
            stage = 'handoff:ready'
            await self._bounded(stage, lambda: self._event('handoff_ready',
                self._handoff_decision_id), context='commit')
            self._set_handoff_deadline(self.deps.settings.engine.handoff.join_timeout_s, 'join_timeout')
        except _CommitStopped:
            return
        except (Exception, asyncio.CancelledError) as error:
            if self.ended:
                return
            if not isinstance(error, _CommitFailed):
                self._record_failure(stage, type(error).__name__)
            self._failure_hangup = asyncio.create_task(self.hangup('handoff_failed'))

    async def join(self) -> JoinCallOut:
        async with self._handoff_lock:
            if self.status().phase != 'handoff_ready' or self._bridge is not None or self._handoff_expired():
                if self._handoff_expired() and not self.ended:
                    await self.hangup(self._handoff_timeout_reason)
                raise ToolFailure('handoff_not_ready')
            if self._handoff_id is None:
                self._handoff_id = uuid.uuid4().hex
                self._handoff_decision_id = self._toolbox.handoff_decision_id
            self._handoff_id = uuid.uuid4().hex
            self._handoff_generation += 1
            self.deps.handoff_tokens.revoke(self.call_id)
            if self._handoff_deadline_ms is not None:
                self._arm_handoff_timer()
            token, expiry = self.deps.handoff_tokens.issue(self.call_id, self._handoff_id)
            return JoinCallOut(task_id=self.task_id, call_id=self.call_id, handoff_id=self._handoff_id,
                ws_path=f'/ws/handoff/{self._handoff_id}', token=token, expires_at=expiry,
                company_name=self.spec.company_name,
                media=m.JoinMedia(ai_media='disconnected', recording=False, transcription=False))

    async def return_to_agent(self, handoff_id: str) -> ReturnOut:
        # TextCall owns the old ReturnOut contract. Live never reconnects AI.
        raise ToolFailure('no_active_handoff')

    async def attach_handoff(self, websocket: Any, handoff_id: str) -> None:
        async with self._handoff_lock:
            if (self.ended or self.status().phase != 'handoff_ready' or handoff_id != self._handoff_id
                    or self._bridge is not None or self._handoff_expired()):
                raise ToolFailure('no_active_handoff')
            generation = self._handoff_generation
            bridge = HandoffBridge(websocket=websocket, media_gate=self._media_gate, company_name=self.spec.company_name)
            self._bridge = bridge
            self._media_gate.attach_bridge(bridge)
            bridge.live_no_resume = True
            async def ready():
                # Called after validating hello and before sending ready. Check
                # again after the send: the authoritative deadline wins ties.
                if generation != self._handoff_generation or self.ended or self._handoff_expired():
                    return False
                return True
            async def started():
                async with self._handoff_lock:
                    if not await ready():
                        return False
                    self._handoff_ever_joined = True
                    self._handoff_deadline_ms = None
                    self._handoff_expires_at = None
                    if self._handoff_timer:
                        self._handoff_timer.cancel()
                    self.deps.handoff_tokens.revoke(self.call_id)
                    self._toolbox.action_phase = self._phase = 'in_handoff'
                    self._event('handoff_started', self._handoff_decision_id,
                        {'generation': generation, 'ready_ms': self.deps.clock.monotonic_ms()})
                    # Register the accepted generation before releasing the lock.
                    # These tasks only persist; they never wait for call teardown.
                    confirmation = asyncio.create_task(self._confirm_handoff_connected(generation))
                    self._handoff_confirmations.add(confirmation)
                await asyncio.shield(confirmation)
                return True
            bridge.before_ready, bridge.on_ready = ready, started
        reason = await bridge.run()
        if reason == 'user_hangup':
            await self.hangup('user_hangup')
            return
        async with self._handoff_lock:
            if self.ended or generation != self._handoff_generation or self._bridge is not bridge:
                return
            self._bridge = None
            self._media_gate.attach_bridge(None)
            self.deps.handoff_tokens.revoke(self.call_id)
            self._handoff_generation += 1
            self._toolbox.action_phase = self._phase = 'handoff_ready'
            if self._handoff_ever_joined and self._handoff_deadline_ms is None:
                self._set_handoff_deadline(self.deps.settings.engine.handoff.rejoin_grace_s, 'rejoin_timeout')
            elif self._handoff_deadline_ms is not None:
                self._arm_handoff_timer()

    async def _confirm_handoff_connected(self, generation: int) -> None:
        callback = self.deps.on_handoff_connected
        if callback is None:
            return
        policy = self.deps.settings.engine.handoff
        last_error = None
        for _ in range(policy.confirm_attempts):
            try:
                await asyncio.wait_for(callback(self.call_id), timeout=policy.confirm_timeout_s)
            except (Exception, asyncio.CancelledError) as error:
                last_error = type(error).__name__
            else:
                return
        self._handoff_confirmation_failed = True
        self._event("error", action={"code": "handoff_confirmation_failed", "generation": generation,
            "attempts": policy.confirm_attempts, "error_type": last_error})

    async def hangup(self, reason: str) -> None:
        async with self._hangup_lock:
            if self.ended and not self._remote_hangup_pending:
                return
            retry = self.ended
            if not self.ended:
                self._finish_reason = reason
            try:
                if self._call_sid:
                    self._remote_hangup_pending = True
                    await self.carrier.hangup(self._call_sid)
                    self._remote_hangup_pending = False
            finally:
                await self._finish(self.call_id)
                if retry and self._owns_carrier and not self._remote_hangup_pending:
                    await self.carrier.aclose()

    async def _on_line_disconnected(self, transport: Any, websocket: Any) -> None:
        # Never wait for cancellation inside the transport receive callback: the
        # pipeline needs that receive task to return while processing CancelFrame.
        if not self.ended:
            self._line_disconnected = True
            task = asyncio.create_task(self._finish(self.call_id))
            self._background.add(task)
            task.add_done_callback(self._background.discard)
            task.add_done_callback(self._retrieve_task_exception)

    @property
    def ended(self) -> bool:
        return self._ended_event.is_set()

    @property
    def remote_hangup_pending(self) -> bool:
        return self._remote_hangup_pending

    async def wait_ended(self) -> None:
        await self._ended_event.wait()

    def _event(self, kind: str, decision_id: str | None = None, action: dict | None = None) -> None:
        self.deps.trace.event(m.TraceEvent(task_id=self.task_id, call_id=self.call_id,
            event_type=kind, decision_id=decision_id, t_ms=self.deps.clock.monotonic_ms(), action=action))

    def _gate_fact(self, kind, **fields):
        t_ms = fields.pop('t_ms')
        self.deps.trace.event(m.TraceEvent(task_id=self.task_id, call_id=self.call_id,
            event_type=kind, t_ms=t_ms, action=fields))

    async def _flush_injections(self) -> None:
        while not self.ended:
            await self._injector.flush()
            await self.deps.clock.sleep(self.deps.settings.engine.vad.frame_ms / 1000)

    @staticmethod
    def _retrieve_task_exception(task):
        if not task.cancelled():
            task.exception()

    async def _finish(self, call_id: str) -> None:
        if self._finish_task is None:
            self._finish_task = asyncio.create_task(self._run_finish(call_id))
            self._finish_task.add_done_callback(self._retrieve_task_exception)
        elif self._finish_task.done():
            # Later remote retries do not receive the old result callback error.
            return
        caller = asyncio.current_task()
        if caller.cancelling():
            # A recycled task may enter here for the first time in its finally.
            # Waiting for the owner would make close -> caller -> finish a cycle.
            return
        self._finish_waiters.add(caller)
        try:
            await asyncio.shield(self._finish_task)
        finally:
            self._finish_waiters.discard(caller)


    def _teardown_snapshot(self):
        records = [dict(row) for row in TEARDOWN_STATE.get(self.call_id, [])]
        return {'teardown_errors': records,
                'teardown_unreclaimed': [dict(row) for row in records
                                         if row['error_type'] == 'Unreclaimed']}

    async def _run_finish(self, call_id):
        self._notified = True
        self._ended_event.set()
        self._phase = "ended"
        self.deps.line_tokens.revoke(self.call_id)
        self.deps.handoff_tokens.revoke(self.call_id)
        self._handoff_generation += 1
        async def step(name, fn):
            return await self._bounded(name, fn, context='finish')
        if self._media_gate:
            await step('end_output', self._media_gate.end_output)
        # This wait has no cancellation path to the commit, even on timeout.
        if self._handoff_commit is not None:
            done, _ = await asyncio.wait({self._handoff_commit},
                timeout=self.deps.settings.engine.speaker.teardown_step_timeout_ms / 1000)
            if not done:
                self._record_failure('finish:handoff_commit', 'CommitPending')
        if self._keypad:
            await step('keypad.close', lambda: self._keypad.close(
                exclude=frozenset(self._finish_waiters | {self._finish_task})))
        if self._toolbox:
            await step('toolbox.close', lambda: self._toolbox.close(
                exclude=frozenset(self._finish_waiters | {self._finish_task})))
        if self._tap:
            await step('tap.close', lambda: self._tap.close(
                exclude=frozenset(self._finish_waiters | {self._finish_task})))
        async def background():
            tasks = tuple(task for task in self._background
                if task is not asyncio.current_task() and task not in self._finish_waiters
                and task not in UNRECLAIMED
                and task not in (self._handoff_commit, self._failure_hangup, self._finish_task))
            for task in tasks:
                task.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)
        await step('background', background)
        # Accepted confirmations remain outside the cancellation direction.
        async def confirmations():
            await asyncio.gather(*(asyncio.shield(task) for task in self._handoff_confirmations))
        await step('handoff_confirmations', confirmations)
        try:
            hung_up = any(e.event_type == 'hangup' for e in self.deps.trace.events(call_id=self.call_id))
        except Exception as error:
            self._record_failure('finish:reason', type(error).__name__)
            hung_up = False
        reason = self._finish_reason or ('hangup' if hung_up else 'company_hangup')
        if not hung_up and self._line_disconnected and self._finish_reason is None:
            await step('remote_hangup', lambda: self._event('remote_hangup'))
        if self._media_gate:
            await step('settle_responses', self._media_gate.settle_responses)
        def ended_event():
            self._event('call_ended', action={'reason': reason,
                'handoff_joined': self._handoff_ever_joined, 'live': True,
                'system_handoff': self._system_handoff, 'handoff_generation': self._handoff_generation,
                'handoff_confirmation_failed': self._handoff_confirmation_failed,
                **self._teardown_snapshot()})
        await step('call_ended', ended_event)
        if self._bridge:
            await step('bridge.close', lambda: self._bridge.close(reason if reason in
                ('company_hangup', 'user_hangup', 'join_timeout', 'rejoin_timeout') else 'user_hangup'))
        if self._listen_hub:
            await step('listen_hub.close', lambda: self._listen_hub.close('call_ended'))
        if self._task:
            await step('task.cancel', self._task.cancel)
        callback_error = None
        try:
            await self._bounded('on_ended', lambda: self.deps.on_ended(call_id),
                context='finish', propagate_error=True)
        except Exception as error:
            callback_error = error
        if self._owns_carrier and not self._remote_hangup_pending:
            await step('carrier.aclose', self.carrier.aclose)
        try:
            self._event('action', action={'kind': 'teardown_summary', **self._teardown_snapshot()})
        except Exception:
            pass
        if callback_error is not None:
            raise callback_error


def create_live_call(spec: CallSpec, deps: CallDeps) -> LiveCall:
    """interfaces.CallSessionFactory（live-sim）。"""
    return LiveCall(spec, deps)
