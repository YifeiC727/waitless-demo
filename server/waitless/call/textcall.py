"""Text calls use the shared decision gate without loading any audio stack."""

import asyncio
from contextlib import suppress
from datetime import datetime
from string import Formatter
from typing import Any
from uuid import uuid4

from pydantic import ValidationError
from starlette.status import WS_1002_PROTOCOL_ERROR
from starlette.websockets import WebSocketDisconnect, WebSocketState

from waitless import models as m
from waitless.interfaces import CallDeps, CompanyEngine, ToolFailure, UnknownDecision
from waitless.models import (
    ActionRecord, AnswerChoiceOut, CallSpec, CallStatusOut, CompanyLine,
    DecisionResult, JoinCallOut, ReturnOut,
)

__all__ = ["TextActionBackend", "TextCall", "create_text_call"]


class TextActionBackend:
    """Translate recorded decisions into inputs to the text company."""

    def __init__(self, *, call: "TextCall", deps: CallDeps, company: CompanyEngine) -> None:
        self._call, self._deps, self._company = call, deps, company

    async def execute(self, result: DecisionResult) -> ActionRecord:
        self._require_decision(result)
        started = self._deps.clock.monotonic_ms()
        if self._call.ended:
            raise RuntimeError("Cannot execute an action after call end")
        if self._call._phase in ("handoff_ready", "in_handoff"):
            raise RuntimeError("Agent actions are suspended during handoff")
        if result.point == "said_as_decided" and result.intent == m.SAID_SAFE_DEFAULT:
            self._call._event("say_mismatch", result.decision_id)
        if result.call_phase is not None:
            self._call._mapped_phase = m.CALL_PHASE_FROM_JEV[result.call_phase]
            if self._call._phase not in m.ACTION_DRIVEN_PHASES:
                self._call._phase = self._call._mapped_phase
        if result.action == "press":
            self._call._event("dtmf_sent", result.decision_id, action={"digits": result.digits})
            self._call._enqueue(self._company.on_digits(result.digits))
        elif result.action == "say":
            await self._say(result.say_text, result.decision_id)
        elif result.action == "release":
            key, text = self._open_answer(result.turn_seq)
            await self._say(text, result.decision_id, template=key)
        elif result.action == "ask_user":
            await self._ask_user(result)
        elif result.action == "handoff":
            await self._say_template("handoff.announce", result.decision_id)
            if not self._call.ended:
                self._call._event("handoff_announced", result.decision_id)
                self._call._handoff_id = uuid4().hex
                self._call._handoff_decision = result.decision_id
                self._call._handoff_done = asyncio.Event()
                self._call._handoff_reason = None
                self._call._phase = "handoff_ready"
                self._call._agent_ready.clear()
                self._call._interrupts += 1
                self._call._event("handoff_ready", result.decision_id)
                self._call._event("handoff_started", result.decision_id)
        elif result.action == "end":
            # The terminating template must not recursively start another C2 end.
            await self._say_template("end.goodbye", result.decision_id, check=False)
            await self._call._finish("decision_end", result.decision_id)
        return ActionRecord(decision_id=result.decision_id, action=result.action,
                            started_ms=started, finished_ms=self._deps.clock.monotonic_ms(),
                            ok=True, detail=result.reason_code)

    def _require_decision(self, result: DecisionResult) -> None:
        if not self._deps.trace.has_decision(result.decision_id):
            raise UnknownDecision(result.decision_id)

    def _template(self, key: str, **extra: str) -> str:
        # Never read or serialize Store.get_profile(). Only the compiled whitelist
        # and explicit task-card data are available to template interpolation.
        values = {**self._call._spec.compiled.allowed_fields,
                  "effective_date": self._call._spec.compiled.task_card.effective_date,
                  **extra}
        template = self._call._spec.compiled.say_templates[key]
        for _, field, _, _ in Formatter().parse(template):
            if field is not None and (field in m.FORBIDDEN_FIELD_KEYS or
                                      field not in values or values[field] is None):
                raise ValueError(f"Unavailable template field: {field}")
        return template.format_map(values)

    def _open_answer(self, turn_seq: int | None) -> tuple[str, str]:
        turn = self._call._turns.get(turn_seq)
        compiled = self._call._spec.compiled
        if turn is not None:
            for cue_key, cues in compiled.lexicons.items():
                if not cue_key.startswith(m.FIELD_CUES_PREFIX):
                    continue
                field = cue_key[len(m.FIELD_CUES_PREFIX):]
                key = f"open_answer.{field}"
                if field in m.FORBIDDEN_FIELD_KEYS or key not in compiled.say_templates:
                    continue
                if any(cue.casefold() in turn.text.casefold() for cue in cues):
                    try:
                        return key, self._template(key)
                    except (KeyError, ValueError):
                        break
        return "ask_repeat", self._template("ask_repeat")

    async def _say_template(self, key: str, decision_id: str, *, check: bool = True,
                            **values: str) -> None:
        await self._say(self._template(key, **values), decision_id, template=key, check=check)

    async def _say(self, text: str, decision_id: str, *, template: str | None = None,
                   check: bool = True) -> None:
        attempt = 0
        while not self._call.ended:
            self._call._event("injection_sent", decision_id,
                              action={"text": text, "template": template})
            self._call._event("waitless_utterance", decision_id,
                              speaker="waitless", transcript=text)
            self._call._append_transcript("waitless", text)
            self._call._enqueue(self._company.on_speech(text))
            if not check:
                return
            request = m.DecisionRequest(
                decision_id=uuid4().hex, point="said_as_decided",
                task_id=self._call.task_id, call_id=self._call.call_id,
                turn=None, candidates=[], context=self._call._context(),
                transcript=list(self._call._transcript),
                template_vars={m.SAID_SUMMARY_VAR: text}, attempt=attempt,
            )
            result = await self._deps.gate.decide("said_as_decided", request)
            self._require_decision(result)
            if result.action == "wait":
                return
            if result.intent == m.SAID_REINJECT:
                text, decision_id, template = result.say_text, result.decision_id, None
                attempt += 1
                continue
            # C2 policy owns the fallback action, including its trace link.
            if result.action == "say":
                self._call._event("say_mismatch", result.decision_id)
                await self._say(result.say_text, result.decision_id, check=False)
            else:
                await self.execute(result)
            return

    async def _ask_user(self, result: DecisionResult) -> None:
        turn = self._call._turns.get(result.turn_seq)
        question = turn.text if turn else ""
        policy = self._call._spec.compiled.policy["global"]["ask_user"]
        try:
            offers = await self._deps.llm.extract_offer_options(question)
            options = [m.Option(id=uuid4().hex, label=o.label, detail=o.detail)
                       for o in offers[:policy["max_options"]]]
            if not options:
                raise ValueError("No extracted offers")
        except Exception:
            options = [m.Option(id=name, label=name.replace("_", " "), detail="")
                       for name in ("accept", "decline", "decide_later")][:policy["max_options"]]
        choice = self._deps.pending.create(
            call_id=self._call.call_id, decision_id=result.decision_id,
            question_text=question, options=options, timeout_s=policy["timeout_sec"],
        )
        self._call._choice_ids.add(choice.choice_id)
        self._call._choice_answer = asyncio.get_running_loop().create_future()
        self._call._phase = "awaiting_user"
        self._call._interrupts += 1
        self._call._event("pending_choice_created", result.decision_id,
                          action={"choice_id": choice.choice_id})
        await self._say_template("handoff.ask_user", result.decision_id)
        # Stop waiting, but leave the synchronous reply batch to the normal loop.
        if self._call.ended or self._company.ended or self._call._company_hangup_queued:
            self._call._choice_answer.cancel()
            self._call._choice_answer = None
            if not self._call.ended:
                self._call._phase = self._call._mapped_phase
            return
        delay = max(0, (choice.expires_at - datetime.fromisoformat(self._deps.clock.now_iso())).total_seconds())
        timer = asyncio.create_task(self._deps.clock.sleep(delay))
        try:
            done, _ = await asyncio.wait((timer, self._call._choice_answer),
                                         return_when=asyncio.FIRST_COMPLETED)
            # Resolve the deadline with the registry even if answer and timeout race.
            if self._call._choice_answer in done:
                option_id = self._call._choice_answer.result()
                current = self._deps.pending.get(choice.choice_id)
            else:
                self._deps.pending.expire_due()
                current, option_id = self._deps.pending.get(choice.choice_id), None
            self._call._phase = self._call._mapped_phase
            if current.status == "answered" and option_id is not None:
                selected = next(o for o in choice.options if o.id == option_id)
                key = self._call._question_key(question)
                if key is not None:
                    self._call._answers.append(m.PriorChoice(question_key=key, choice=option_id,
                                                           source="user_answer"))
                await self._say_template("pending.answer", result.decision_id,
                                         option_label=selected.label, option_detail=selected.detail)
            else:
                self._call._event("pending_expired", result.decision_id,
                                  action={"choice_id": choice.choice_id})
                await self._say_template("pending.safe_default", result.decision_id)
        finally:
            timer.cancel()
            with suppress(asyncio.CancelledError):
                await timer
            if self._call._choice_answer is not None:
                self._call._choice_answer.cancel()
            self._call._choice_answer = None


class TextCall:
    """A background, sequential decision loop paced only by the injected clock."""

    def __init__(self, spec: CallSpec, deps: CallDeps, company: CompanyEngine) -> None:
        self._spec, self._deps, self._company = spec, deps, company
        self._backend = TextActionBackend(call=self, deps=deps, company=company)
        self._queue: asyncio.Queue[CompanyLine] = asyncio.Queue()
        self._company_hangup_queued = False
        self._lines: list[CompanyLine] = []
        self._turns: dict[int, m.CompanyTurn] = {}
        self._transcript: list[m.TranscriptLine] = []
        self._tail: list[m.TranscriptTailItem] = []
        self._answers: list[m.PriorChoice] = []
        self._choice_ids: set[str] = set()
        self._choice_answer: asyncio.Future[str] | None = None
        self._phase = "idle"
        self._mapped_phase = "dialing"
        self._interrupts = 0
        self._started_ms: int | None = None
        self._finished_ms: int | None = None
        self._ended = False
        self._done = asyncio.Event()
        self._agent_ready = asyncio.Event()
        self._agent_ready.set()
        self._worker: asyncio.Task[None] | None = None
        self._handoff_id: str | None = None
        self._handoff_decision: str | None = None
        self._handoff_done = asyncio.Event()
        self._handoff_reason: str | None = None
        self._handoff_reasons: dict[str, str] = {}
        self._failure: Exception | None = None

    @property
    def call_id(self) -> str:
        return self._spec.call_id

    @property
    def task_id(self) -> str:
        return self._spec.task_id

    @property
    def scenario_id(self) -> str | None:
        return self._spec.scenario_id

    async def start(self) -> None:
        if self._worker is not None or self._ended:
            return
        self._started_ms = self._deps.clock.monotonic_ms()
        self._phase = "dialing"
        self._enqueue(self._company.start())
        self._event("call_started", skill_versions=self._spec.compiled.skill_versions)
        self._worker = asyncio.create_task(self._run())

    def _enqueue(self, lines: list[CompanyLine]) -> None:
        if not self._ended:
            for line in lines:
                if line.kind == "hangup":
                    self._company_hangup_queued = True
                self._queue.put_nowait(line)

    async def _run(self) -> None:
        try:
            while not self._ended:
                if self._company.ended and (
                    self._queue.empty() or self._phase in ("handoff_ready", "in_handoff")
                ):
                    await self._finish("company_hangup")
                    break
                await self._agent_ready.wait()
                if self._ended:
                    break
                line = await self._queue.get()
                started = self._deps.clock.monotonic_ms()
                await self._deps.clock.sleep(self._deps.settings.engine.textsim.pace_ms / 1000)
                if self._ended:
                    break
                self._lines.append(line)
                if line.kind == "hangup":
                    await self._finish("company_hangup")
                    break
                options = []
                if self._spec.compiled.press_patterns:
                    from waitless.decision import menu_parse
                    options = menu_parse.parse_menu(
                        line.text, self._spec.compiled.press_patterns,
                        digit_words=self._spec.compiled.digit_words or None,
                    )
                seq = len(self._turns)
                turn = m.CompanyTurn(call_id=self.call_id, seq=seq, t_start_ms=started,
                                     t_end_ms=self._deps.clock.monotonic_ms(), text=line.text,
                                     kind="menu" if options else "speech", menu_options=options)
                self._turns[seq] = turn
                self._event("company_turn", speaker="company", transcript=line.text,
                            action={"kind": turn.kind, "seq": seq}, t_ms=turn.t_end_ms)
                self._append_transcript("company", line.text)
                if self._phase == "resuming":
                    self._phase = self._mapped_phase
                ctx = m.RoundContext(call_id=self.call_id, task_id=self.task_id,
                                     compiled=self._spec.compiled, context=self._context(),
                                     transcript=list(self._transcript))
                result = await self._deps.gate.decide_round(turn, ctx)
                await self._backend.execute(result)
                if self._phase not in m.ACTION_DRIVEN_PHASES and not self._ended:
                    self._phase = self._mapped_phase
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            self._failure = exc
            # Exception messages can contain request data. Keep only the type in trace.
            self._event("error", action={"error_type": type(exc).__name__})
            await self._finish("execution_failed")

    def _event(self, event_type: str, decision_id: str | None = None, **values: Any) -> None:
        self._deps.trace.event(m.TraceEvent(task_id=self.task_id, call_id=self.call_id,
                                           event_type=event_type, decision_id=decision_id, **values))

    def _append_transcript(self, speaker: str, text: str) -> None:
        self._transcript.append(m.TranscriptLine(speaker=speaker, text=text))
        self._tail.append(m.TranscriptTailItem(speaker=speaker, text=text,
                                               t_ms=self._deps.clock.monotonic_ms()))

    def _context(self) -> m.DecisionContext:
        compiled = self._spec.compiled
        prior = [m.PriorChoice(question_key=d.question_id, choice=d.selected, source="task_card")
                 for d in compiled.task_card.precall_decisions if d.selected is not None]
        prior += self._answers
        prior += [m.PriorChoice.from_preference(p) for p in
                  self._deps.store.list_preferences(compiled.task_card.company.company_id)]
        return m.DecisionContext(task_card=compiled.task_card, allowed_fields=dict(compiled.allowed_fields),
                                 standing_rules=self._deps.store.list_standing_rules(),
                                 prior_choices=prior, interrupts_used=self._interrupts, phase=self._phase)

    def _question_key(self, question: str) -> str | None:
        compiled = self._spec.compiled
        for key, cues in compiled.lexicons.items():
            if not key.startswith(m.FIELD_CUES_PREFIX) and any(c.casefold() in question.casefold() for c in cues):
                return key
        for decision in compiled.task_card.precall_decisions:
            if decision.question.casefold() in question.casefold():
                return decision.question_id
        return None

    def status(self, debug: bool = False) -> CallStatusOut:
        handoff = None
        if self._phase in ("handoff_ready", "in_handoff"):
            handoff = m.HandoffStatus(state="ready" if self._phase == "handoff_ready" else "active",
                                     company_name=self._spec.company_name,
                                     media=m.Media(ai_media="disconnected", recording=False, transcription=False))
        info = None
        if debug:
            last = next((e for e in reversed(self._deps.trace.events(task_id=self.task_id))
                         if e.event_type == "decision"), None)
            decision = None
            if last is not None:
                policy = last.policy_decision or {}
                decision = m.LastDecision(decision_id=last.decision_id, point=last.decision_point,
                                          intent=policy["intent"], action=policy["action"],
                                          reason_code=policy["reason_code"],
                                          jev=(last.jev_response or {}).get("answers", {}),
                                          latency_ms=last.latency_ms)
            skills = [m.SkillCommit(name=s.name, commit=s.commit)
                      for s in self._spec.compiled.skill_versions]
            info = m.CallStatusDebug(mode=self._spec.mode, skills=skills,
                                     last_decision=decision, guard=m.GuardCounters(blocked=0, released=0))
        elapsed = 0 if self._started_ms is None else max(0, (
            (self._finished_ms if self._finished_ms is not None else self._deps.clock.monotonic_ms())
            - self._started_ms) / 1000)
        return CallStatusOut(task_id=self.task_id, call_id=self.call_id, phase=self._phase,
                             status_line=self._phase.replace("_", " "),
                             transcript_tail=[] if self._phase == "in_handoff" else list(self._tail[-8:]),
                             pending_choice=self._deps.pending.current(self.call_id), handoff=handoff,
                             interrupts_used=self._interrupts, elapsed_s=elapsed, ended=self._ended, debug=info)

    async def answer(self, choice_id: str, option_id: str) -> AnswerChoiceOut:
        if choice_id not in self._choice_ids:
            raise ToolFailure("unknown_choice", choice_id)
        existing = self._deps.pending.get(choice_id)
        if existing.status == "answered":
            raise ToolFailure("unknown_choice", "Choice already answered")
        choice = self._deps.pending.answer(choice_id, option_id)
        if choice.status == "answered" and self._choice_answer is not None and not self._choice_answer.done():
            self._choice_answer.set_result(option_id)
            self._event("user_answered", action={"choice_id": choice_id, "option_id": option_id})
        return AnswerChoiceOut(task_id=self.task_id, choice_id=choice_id,
                               status="expired" if choice.status == "expired" else "choice_recorded",
                               option_id=option_id)

    async def join(self) -> JoinCallOut:
        if self._phase != "handoff_ready" or self._ended:
            raise ToolFailure("handoff_not_ready", self.call_id)
        token, expires = self._deps.handoff_tokens.issue(self.call_id, self._handoff_id)
        self._phase = "in_handoff"
        # These lines are private to the account holder: no trace, gate or transcript.
        lines = self._company.on_handoff(True)
        self._lines.extend(lines)
        # The decision loop is suspended, so it cannot consume this hangup.
        if self._company.ended or any(line.kind == "hangup" for line in lines):
            await self.hangup("company_hangup")
        return JoinCallOut(task_id=self.task_id, call_id=self.call_id, handoff_id=self._handoff_id,
                           ws_path=f"/ws/handoff/{self._handoff_id}", token=token, expires_at=expires,
                           company_name=self._spec.company_name,
                           media=m.JoinMedia(ai_media="disconnected", recording=False, transcription=False))

    async def return_to_agent(self, handoff_id: str) -> ReturnOut:
        if self._phase != "in_handoff" or self._handoff_id != handoff_id or self._ended:
            raise ToolFailure("no_active_handoff", handoff_id)
        lines = self._company.on_handoff(False)
        self._event("handoff_ended", self._handoff_decision, action={"dropped_sonic_outputs": 0})
        self._phase = "resuming"
        self._handoff_reason = "returned"
        self._handoff_reasons[handoff_id] = "returned"
        self._handoff_done.set()
        await self._backend._say_template("handoff.resume", self._handoff_decision)
        if not self._ended:
            self._event("resume_injected", self._handoff_decision)
        self._enqueue(lines)
        self._agent_ready.set()
        return ReturnOut(task_id=self.task_id, call_id=self.call_id, status="resuming",
                         media=m.ReturnMedia(ai_media="connected", recording=False, transcription=True))

    async def attach_handoff(self, websocket: Any, handoff_id: str) -> None:
        # A synchronous text-company batch can end between join and browser hello.
        completed = handoff_id in self._handoff_reasons
        if handoff_id != self._handoff_id or (self._phase != "in_handoff" and not completed):
            raise ToolFailure("no_active_handoff", handoff_id)
        hello = await websocket.receive()
        if hello["type"] == "websocket.disconnect":
            return
        try:
            if not isinstance(hello.get("text"), str):
                raise ValueError("Handoff hello must be a text frame")
            m.HandoffHello.model_validate_json(hello["text"])
        except (ValidationError, ValueError):
            try:
                await websocket.close(code=WS_1002_PROTOCOL_ERROR)
            except WebSocketDisconnect:
                pass
            except RuntimeError:
                if (getattr(websocket, "client_state", None) != WebSocketState.DISCONNECTED
                        and getattr(websocket, "application_state", None) != WebSocketState.DISCONNECTED):
                    raise
            return
        await websocket.send_json(m.HandoffReady(type="ready", company_name=self._spec.company_name,
                                  media=m.JoinMedia(ai_media="disconnected", recording=False,
                                                    transcription=False)).model_dump(mode="json"))
        finished = asyncio.create_task(self._handoff_done.wait())
        receive = asyncio.create_task(websocket.receive())
        try:
            while True:
                done, _ = await asyncio.wait((finished, receive), return_when=asyncio.FIRST_COMPLETED)
                if finished in done:
                    reason = self._handoff_reasons[handoff_id]
                    await websocket.send_json(m.HandoffEnded(type="ended", reason=reason).model_dump(mode="json"))
                    await websocket.close()
                    return
                if receive.result()["type"] == "websocket.disconnect":
                    return
                # Drop all incoming audio without decoding or saving it.
                receive = asyncio.create_task(websocket.receive())
        finally:
            for task in (finished, receive):
                task.cancel()
                with suppress(asyncio.CancelledError):
                    await task

    async def _finish(self, reason: str, decision_id: str | None = None) -> None:
        if self._ended:
            return
        self._ended = True
        if self._phase in ("handoff_ready", "in_handoff"):
            self._event("handoff_ended", self._handoff_decision, action={"dropped_sonic_outputs": 0})
            self._handoff_reasons[self._handoff_id] = "company_hangup"
        self._phase = "ended"
        self._finished_ms = self._deps.clock.monotonic_ms()
        self._handoff_reason = "company_hangup"
        self._handoff_done.set()
        if decision_id is not None:
            self._event("hangup", decision_id, action={"reason": reason})
        elif reason == "company_hangup":
            self._event("remote_hangup")
        self._event("call_ended", action={"reason": reason})
        try:
            await self._deps.on_ended(self.call_id)
        finally:
            self._done.set()

    async def hangup(self, reason: str) -> None:
        await self._finish(reason)
        if self._worker is not None and self._worker is not asyncio.current_task():
            self._worker.cancel()
            with suppress(asyncio.CancelledError):
                await self._worker

    @property
    def ended(self) -> bool:
        return self._ended

    async def wait_ended(self) -> None:
        await self._done.wait()
        if self._failure is not None:
            raise self._failure

    def lines(self) -> list[CompanyLine]:
        return list(self._lines)


def create_text_call(spec: CallSpec, deps: CallDeps) -> TextCall:
    if deps.company_factory is not None:
        company = deps.company_factory(spec.scenario_id)
    else:
        from waitless.company.config import load_company, load_scenario
        from waitless.company.text import TextCompany
        company_id = spec.compiled.task_card.company.company_id
        company = TextCompany(
            load_scenario(deps.settings.company_dir, company_id, spec.scenario_id),
            load_company(deps.settings.company_dir, company_id),
        )
    return TextCall(spec, deps, company)
