"""Phone-agent tool boundaries: validate arguments and carry out AI-requested effects."""
import asyncio
import json
import re
import uuid
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path

import yaml
from pipecat.services.llm_service import FunctionCallParams
from pipecat.adapters.schemas.function_schema import FunctionSchema
from pipecat.adapters.schemas.tools_schema import ToolsSchema
from pipecat.frames.frames import (OutputAudioRawFrame, OutputDTMFUrgentFrame,
    LLMFullResponseStartFrame, LLMFullResponseEndFrame)
from pipecat.processors.frame_processor import FrameProcessor, FrameDirection
from waitless import models as m
from waitless.audio.codec import silence_pcm16
from waitless.decision.jev import build_state
from waitless.skills import loader

TOOL_NAMES = ('update_status', 'ask_user', 'ask_jev', 'request_handoff', 'end_call')
AGENT_EVENT_PREFIX = '[Waitless event] '
AGENT_PHASES = ('ivr', 'hold', 'ai_agent', 'live_rep', 'wrapping_up')


def load_tool_error_hints(skills_dir) -> dict[str, str]:
    """Read skill-owned tool guidance once when a call is set up."""
    path = Path(skills_dir) / 'phone-agent/references/tool-errors.yaml'
    try:
        data = yaml.safe_load(path.read_text(encoding='utf-8'))
    except yaml.YAMLError:
        raise ValueError('invalid tool error hints YAML') from None
    if not isinstance(data, dict) or set(data) != {'errors'}:
        raise ValueError('tool error hints must contain only an errors mapping')
    hints = data['errors']
    if not isinstance(hints, dict) or any(
            not isinstance(code, str) or not code.strip()
            or not isinstance(hint, str) or not hint.strip()
            for code, hint in hints.items()):
        raise ValueError('tool error hints must map nonempty codes to nonempty strings')
    return hints


def _object(properties, required):
    return {'type': 'object', 'properties': properties, 'required': required}


TOOL_PARAMETERS = {
    'update_status': _object({'phase': {'type': 'string', 'enum': list(AGENT_PHASES)},
        'status_line': {'type': 'string', 'maxLength': 120}}, ['phase', 'status_line']),
    'ask_user': _object({'question': {'type': 'string', 'maxLength': 200},
        'options': {'type': 'array', 'minItems': 2, 'maxItems': 3,
            'items': _object({'label': {'type': 'string', 'maxLength': 60},
                'detail': {'type': 'string', 'maxLength': 120}}, ['label'])}}, ['question', 'options']),
    'ask_jev': _object({'question': {'type': 'string', 'maxLength': 300},
        'options': {'type': 'array', 'minItems': 2, 'maxItems': 5,
            'items': {'type': 'string', 'maxLength': 80}}}, ['question', 'options']),
    'request_handoff': _object({'reason': {'type': 'string', 'maxLength': 160}}, ['reason']),
    'end_call': _object({'outcome': {'type': 'string', 'enum': ['confirmed', 'needs_follow_up', 'failed']},
        'confirmation_number': {'type': ['string', 'null']},
        'summary': {'type': 'string', 'maxLength': 300}}, ['outcome', 'summary']),
}


def event_text(payload: dict) -> str:
    return AGENT_EVENT_PREFIX + json.dumps(payload, separators=(',', ':'), sort_keys=True)


async def send_dtmf(pipeline_task, settings, digits):
    """Send one key sequence and a tail that OutputMonitor excludes from speech."""
    await pipeline_task.queue_frame(OutputDTMFUrgentFrame.from_string(digits))
    engine = settings.engine
    audio = silence_pcm16(engine.dtmf.tail_silence_ms, engine.audio.pipeline_in_sample_rate)
    if audio:
        tail = OutputAudioRawFrame(audio=audio, sample_rate=engine.audio.pipeline_in_sample_rate, num_channels=1)
        tail._waitless_dtmf_tail = True
        await pipeline_task.queue_frame(tail)


def _valid(value, schema):
    types = schema['type'] if isinstance(schema['type'], list) else [schema['type']]
    kind = ('null' if value is None else 'string' if isinstance(value, str) else
            'object' if isinstance(value, dict) else 'array' if isinstance(value, list) else None)
    if kind not in types or ('enum' in schema and value not in schema['enum']):
        return False
    if kind == 'string':
        return (len(value) <= schema.get('maxLength', len(value))
                and ('pattern' not in schema or re.fullmatch(schema['pattern'], value) is not None))
    if kind == 'array':
        return (schema.get('minItems', 0) <= len(value) <= schema.get('maxItems', len(value))
                and all(_valid(item, schema['items']) for item in value))
    if kind == 'object':
        props = schema['properties']
        return (set(schema['required']) <= value.keys() and value.keys() <= props.keys()
                and all(_valid(item, props[key]) for key, item in value.items()))
    return True


class OutputMonitor(FrameProcessor):
    """Observe Sonic audio without delaying or suppressing a frame."""
    def __init__(self, *, clock, media_gate=None):
        super().__init__(enable_direct_mode=True)
        self.clock, self.media_gate = clock, media_gate
        self.audio_serial = 0
        self.last_audio_ms = None
        self.dropped_in_handoff = 0
        self.response_number = 0
        self.response_observers = []
        self.handoff_observers = []

    async def process_frame(self, frame, direction):
        await super().process_frame(frame, direction)
        if direction == FrameDirection.DOWNSTREAM:
            if isinstance(frame, LLMFullResponseStartFrame):
                self.response_number += 1
            elif isinstance(frame, LLMFullResponseEndFrame):
                for observer in self.response_observers:
                    await observer(self.response_number, self.clock.monotonic_ms())
            if isinstance(frame, OutputAudioRawFrame) and not getattr(frame, '_waitless_dtmf_tail', False):
                self.audio_serial += 1
                self.last_audio_ms = self.clock.monotonic_ms()
            if (isinstance(frame, LLMFullResponseStartFrame) and self.media_gate
                    and self.media_gate.in_handoff):
                self.dropped_in_handoff += 1
        await self.push_frame(frame, direction)

    async def wait_idle(self, idle_ms: int, cap_ms: int = 8000):
        started = self.clock.monotonic_ms()
        while True:
            now = self.clock.monotonic_ms()
            last = self.last_audio_ms if self.last_audio_ms is not None else started
            remaining = min(idle_ms - (now - last), cap_ms - (now - started))
            if remaining <= 0:
                return
            await self.clock.sleep(remaining / 1000)


@dataclass
class _HeldResult:
    tool: str
    tool_call_id: str
    started_ms: int
    release: asyncio.Future = field(default_factory=lambda: asyncio.get_running_loop().create_future())
    local_done: asyncio.Event = field(default_factory=asyncio.Event)


class AgentToolbox:
    def __init__(self, *, spec, deps, pipeline_task, media_gate, monitor, injector, carrier, call_sid,
                 error_hints=None):
        self.spec, self.deps = spec, deps
        self.pipeline_task, self.media_gate, self.monitor = pipeline_task, media_gate, monitor
        self.injector, self.carrier, self.call_sid = injector, carrier, call_sid
        self._error_hints = error_hints if error_hints is not None else {}
        self.phase = 'dialing'
        self.status_line = None
        self.action_phase = None
        self.handoff_decision_id = None
        self.prior_answers = []
        self._tasks = set()
        self._invocations = set()
        self._held = {}
        self._speech_gen = 0
        self._speech_event = asyncio.Event()
        self._seen_tools = set()
        self._sonic = None
        self._close_lock = asyncio.Lock()
        self._budget_lock = asyncio.Lock()
        self._notified_choices = set()
        self._ending = False
        self._closed = False
        self._handoff_active = False
        self._handoff_dropped_start = 0

    @property
    def interrupts_used(self):
        used = sum(e.event_type in ('pending_choice_created', 'handoff_ready') for e in self._events())
        return used + int(self._handoff_active and self.action_phase not in ('handoff_ready', 'in_handoff'))

    def schema(self) -> ToolsSchema:
        return ToolsSchema(standard_tools=[FunctionSchema(name=name,
            description=self.spec.compiled.agent_tool_descriptions.get(name, ''),
            properties=TOOL_PARAMETERS[name]['properties'], required=TOOL_PARAMETERS[name]['required'])
            for name in TOOL_NAMES])

    def register(self, llm):
        self._sonic = llm
        for name in TOOL_NAMES:
            async def handler(params: FunctionCallParams, name=name):
                await self._invoke(name, params)
            llm.register_function(name, handler, cancel_on_interruption=False)

    def _events(self):
        return self.deps.trace.events(call_id=self.spec.call_id)

    def _event(self, kind, decision_id=None, action=None, **kwargs):
        self.deps.trace.event(m.TraceEvent(task_id=self.spec.task_id, call_id=self.spec.call_id,
            event_type=kind, decision_id=decision_id, t_ms=self.deps.clock.monotonic_ms(),
            action=action, **kwargs))

    def _decision(self, name, args, started, *, request=None, response=None):
        decision_id = uuid.uuid4().hex
        seq = next((e.action['seq'] for e in reversed(self._events()) if e.event_type == 'company_turn'), 0)
        self._event('decision', decision_id, {'turn_seq': seq}, decision_point='agent_tool',
            jev_request=request or {'tool': name, 'arguments': args},
            jev_response=response or {'answers': {}},
            policy_decision={'intent': name, 'action': {'ask_user': 'ask_user',
                'ask_jev': 'wait', 'request_handoff': 'handoff', 'end_call': 'end'}[name],
                'reason_code': 'agent_tool', 'policy_rule': 'phone-agent'},
            latency_ms=self.deps.clock.monotonic_ms() - started)
        return decision_id

    def note_speech(self):
        """Broadcast a committed FINAL without blocking the provider listener."""
        self._speech_gen += 1
        self._seen_tools.clear()
        previous, self._speech_event = self._speech_event, asyncio.Event()
        previous.set()

    def _can_hold(self):
        if (self._closed or self._handoff_active or self.media_gate.in_handoff
                or self.action_phase in ('handoff_ready', 'in_handoff')):
            return False
        eligible = getattr(self._sonic, 'repeat_hold_allowed', None)
        return eligible is not None and eligible(self.spec.compiled.policy['global']['repeat_hold_ms'])

    def _hint(self, result):
        hint = self._error_hints.get(result.get('error'))
        if hint:
            result.setdefault('hint', hint)
        return result

    def _record_result(self, name, params, result, started):
        self._event('agent_tool_result', action={'tool': name, 'tool_call_id': params.tool_call_id,
            'ok': result.get('ok', True), 'result': result,
            'latency_ms': self.deps.clock.monotonic_ms() - started})

    def _local_done(self, task):
        held = self._held.pop(task, None)
        if held is not None:
            held.local_done.set()

    async def _hold_result(self, name, tool_call_id, gen, speech_event):
        # No await between the caller's final eligibility check and registration.
        held = _HeldResult(name, tool_call_id, self.deps.clock.monotonic_ms())
        self._held[asyncio.current_task()] = held
        self._event('agent_tool_held', action={'tool': name, 'tool_call_id': tool_call_id})
        waiters = []
        reason = 'cancelled'
        try:
            waiters.append(self._spawn(speech_event.wait()))
            waiters.append(self._spawn(self.deps.clock.sleep(
                self.spec.compiled.policy['global']['repeat_hold_ms'] / 1000)))
            await asyncio.wait([*waiters, held.release], return_when=asyncio.FIRST_COMPLETED)
            if held.release.done():
                reason = held.release.result()
            elif self._speech_gen != gen:
                reason = 'spoke'
            else:
                reason = 'timeout'
            return reason
        finally:
            # Cancellation is a local terminal too; no detached callback survives.
            self._event('agent_tool_released', action={'tool': name, 'tool_call_id': tool_call_id,
                'reason': reason, 'held_ms': self.deps.clock.monotonic_ms() - held.started_ms})
            for waiter in waiters:
                waiter.cancel()
            if not held.release.done():
                held.release.cancel()
            await asyncio.gather(*waiters, return_exceptions=True)

    async def _release_holds(self, reason):
        held = tuple(self._held.values())
        for item in held:
            if not item.release.done():
                item.release.set_result(reason)
        # Local result ledger is finalized before any handoff event. This is not
        # a provider wire acknowledgement or a session continuation drain.
        await asyncio.gather(*(item.local_done.wait() for item in held))

    async def _invoke(self, name: str, params: FunctionCallParams):
        args, started = dict(params.arguments), self.deps.clock.monotonic_ms()
        gen, speech_event = self._speech_gen, self._speech_event
        repeated = name != 'end_call' and name in self._seen_tools
        self._seen_tools.add(name)
        self._event('agent_tool_call', action={'tool': name, 'tool_call_id': params.tool_call_id, 'args': args})
        task = asyncio.current_task()
        self._invocations.add(task)
        recorded = False
        try:
            try:
                if self._closed:
                    result = {'ok': False, 'error': 'call_ended'}
                elif not _valid(args, TOOL_PARAMETERS[name]):
                    code = (
                        'invalid_phase' if name == 'update_status' and args.get('phase') not in AGENT_PHASES
                        else 'invalid_arguments')
                    result = {'ok': False, 'error': code}
                else:
                    result = await getattr(self, '_' + name)(args, started)
            except Exception:
                # Tool failures are data, never transport or validation details.
                result = {'ok': False, 'error': 'tool_failed'}
            result = self._hint(result)
            # Execution may have crossed a speech, close, handoff or session edge.
            if self._closed:
                result = self._hint({'ok': False, 'error': 'call_ended'})
            elif repeated and self._speech_gen == gen and self._can_hold():
                reason = await self._hold_result(name, params.tool_call_id, gen, speech_event)
                if reason == 'closed':
                    result = self._hint({'ok': False, 'error': 'call_ended'})
                elif reason == 'timeout' and 'repeated_without_speech' in self._error_hints:
                    result.setdefault('hint', self._error_hints['repeated_without_speech'])
            self._record_result(name, params, result, started)
            recorded = True
            self._local_done(task)
            await params.result_callback(result)
        except asyncio.CancelledError:
            if not recorded:
                self._record_result(name, params, self._hint({'ok': False, 'error': 'cancelled'}), started)
            raise
        finally:
            self._local_done(task)
            self._invocations.discard(task)

    def _budget_error(self):
        budget = self.spec.compiled.policy['global']['interrupt_budget']
        used = self.interrupts_used
        if used >= budget:
            return {'ok': False, 'error': 'interrupt_budget_exhausted', 'used': used, 'budget': budget}
        return None

    async def _update_status(self, args, started):
        self.phase, self.status_line = args['phase'], args['status_line']
        # Resuming lasts until the agent reports its current stage.
        if self.action_phase == 'resuming':
            self.action_phase = None
        self._event('status_updated', action=args)
        return {'ok': True}

    async def _ask_user(self, args, started):
        async with self._budget_lock:
            current = self.deps.pending.current(self.spec.call_id)
            if current is not None and current.choice_id not in self._notified_choices:
                return {'ok': False, 'error': 'choice_already_open'}
            error = self._budget_error()
            if error:
                return error
            try:
                options = [m.Option(id=str(i + 1), label=option['label'], detail=option.get('detail', ''))
                           for i, option in enumerate(args['options'])]
            except ValueError:
                return {'ok': False, 'error': 'invalid_arguments'}
            decision_id = self._decision('ask_user', args, started)
            timeout = self.spec.compiled.policy['global']['ask_user']['timeout_sec']
            choice = self.deps.pending.create(call_id=self.spec.call_id, decision_id=decision_id,
                question_text=args['question'], options=options, timeout_s=timeout)
            self._event('pending_choice_created', decision_id, {'choice_id': choice.choice_id})
            if self.action_phase not in ('handoff_ready', 'in_handoff'):
                self.action_phase = 'awaiting_user'
            self._spawn(self._expire(choice))
            return {'status': 'asked', 'choice_id': choice.choice_id}

    async def _expire(self, choice):
        delay = (choice.expires_at - datetime.fromisoformat(self.deps.clock.now_iso())).total_seconds()
        await self.deps.clock.sleep(max(0, delay))
        retry_budget = 1.0
        # A timer can wake just before the wall-clock deadline. Try to close
        # the registry entry with bounded extra waits, then notify the agent.
        for attempt in range(6):
            current = self.deps.pending.get(choice.choice_id)
            if current is None or current.status not in ('open', 'expired'):
                return
            self.deps.pending.expire_due()
            current = self.deps.pending.get(choice.choice_id)
            if current is not None and current.status == 'expired':
                break
            if current is None or current.status != 'open':
                return
            if attempt == 5 or retry_budget <= 0:
                break
            remaining = (choice.expires_at - datetime.fromisoformat(self.deps.clock.now_iso())).total_seconds()
            if remaining <= 0:
                break
            delay = min(remaining, retry_budget)
            await self.deps.clock.sleep(delay)
            retry_budget -= delay
        if choice.choice_id not in self._notified_choices:
            await self.on_choice_expired(choice)

    async def on_user_answer(self, choice, option_id):
        if choice.choice_id in self._notified_choices:
            return
        self._notified_choices.add(choice.choice_id)
        if self.action_phase == 'awaiting_user':
            self.action_phase = None
        option = next(o for o in choice.options if o.id == option_id)
        self.prior_answers.append(m.PriorChoice(question_key=choice.choice_id, choice=option_id, source='user_answer'))
        await self.injector.send_event(event_text({'event': 'user_choice', 'choice_id': choice.choice_id,
            'status': 'answered', 'option_id': option_id, 'label': option.label, 'detail': option.detail}))

    async def on_choice_expired(self, choice):
        if choice.choice_id in self._notified_choices:
            return
        self._notified_choices.add(choice.choice_id)
        decision_id = self.deps.pending.decision_of(choice.choice_id)
        self._event('pending_expired', decision_id, {'choice_id': choice.choice_id})
        if self.action_phase == 'awaiting_user':
            self.action_phase = None
        await self.injector.send_event(event_text({'event': 'user_choice',
            'choice_id': choice.choice_id, 'status': 'expired'}))

    async def _ask_jev(self, args, started):
        request = {'tool': 'ask_jev', 'arguments': args}
        response = None
        try:
            if self.deps.jev is None:
                raise RuntimeError('unavailable')
            instructions = self.spec.compiled.jev_templates.get('ask_jev', {}).get('instructions')
            if instructions is None:
                skill = loader.load_skills(self.deps.settings.skills_dir)['phone-agent']
                instructions = yaml.safe_load((skill.path / 'references/jev.yaml').read_text(encoding='utf-8'))['ask_jev_instructions']
            criteria = {str(i + 1): opt for i, opt in enumerate(args['options'])}
            question = m.JevQuestion(type='choice', instructions=instructions + '\n\nQuestion: ' + args['question'], criteria=criteria)
            transcript = [m.TranscriptLine(speaker=e.speaker, text=e.transcript)
                for e in self._events() if e.transcript and e.speaker in ('company', 'waitless')]
            state = build_state(task_goal=self.spec.compiled.task_card.goal,
                allowed_fields=self.spec.compiled.allowed_fields,
                standing_rules=self.deps.store.list_standing_rules(),
                prior_choices=[*[m.PriorChoice(question_key=p.question_id, choice=p.selected, source='task_card')
                    for p in self.spec.compiled.task_card.precall_decisions if p.selected is not None],
                    *[m.PriorChoice.from_preference(p) for p in self.deps.store.list_preferences(
                        self.spec.compiled.task_card.company.company_id)], *self.prior_answers],
                transcript=transcript)
            request = {'state': state.model_dump(mode='json'), 'questions': {'q': question.model_dump(mode='json')}}
            answer = await self.deps.jev.ask(state, {'q': question})
            response = answer.model_dump(mode='json')
            q = answer.answers['q']
            result = {'choice': criteria[q.choice], 'confidence': q.confidence,
                'probabilities': {criteria[key]: value for key, value in q.probabilities.items()}}
        except Exception:
            result = {'ok': False, 'error': 'jev_unavailable'}
        self._decision('ask_jev', args, started, request=request, response=response)
        return result

    async def _request_handoff(self, args, started):
        async with self._budget_lock:
            if self.media_gate.in_handoff or self._handoff_active:
                return {'ok': False, 'error': 'handoff_already_active'}
            error = self._budget_error()
            if error:
                return error
            self._handoff_active = True
            await self._release_holds('handoff')
            if not self._closed:
                self._spawn(self._handoff(args, started))
            return {'status': 'handoff_ready'}

    async def _handoff(self, args, started):
        if self._closed:
            return
        await self.monitor.wait_idle(self.deps.settings.engine.handoff.announce_idle_ms)
        if self._closed:
            return
        decision_id = self._decision('request_handoff', args, started)
        self.handoff_decision_id = decision_id
        text = next((e.transcript for e in reversed(self._events()) if e.event_type == 'waitless_utterance'), '')
        self._event('handoff_announced', decision_id, {'text': text})
        self.action_phase = 'handoff_ready'
        self._event('handoff_ready', decision_id)
        self._handoff_dropped_start = self.monitor.dropped_in_handoff
        for observer in self.monitor.handoff_observers:
            observer()
        await self.media_gate.enter_handoff()
        if self._closed:
            return
        self._event('handoff_started', decision_id)

    async def on_handoff_returned(self):
        self._handoff_active = False
        self._event('handoff_ended', self.handoff_decision_id,
            {'dropped_sonic_outputs': self.monitor.dropped_in_handoff - self._handoff_dropped_start})
        self.action_phase = 'resuming'
        await self.injector.send_event(event_text({'event': 'handoff_ended'}))

    async def _end_call(self, args, started):
        if not self._ending:
            self._ending = True
            self._spawn(self._end(args, started))
        return {'ok': True}

    async def _end(self, args, started):
        await self.monitor.wait_idle(self.deps.settings.engine.handoff.announce_idle_ms)
        decision_id = self._decision('end_call', args, started)
        self._event('hangup', decision_id, {'outcome': args['outcome'],
            'confirmation_number': args.get('confirmation_number'), 'summary': args['summary']})
        try:
            await self.carrier.hangup(self.call_sid)
        finally:
            await self.deps.on_ended(self.spec.call_id)

    def _spawn(self, coro):
        task = asyncio.create_task(coro)
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)
        return task

    async def close(self):
        async with self._close_lock:
            if self._closed:
                return
            self._closed = True
            await self._release_holds('closed')
            tasks = (self._tasks | self._invocations) - {asyncio.current_task()}
            for task in tasks:
                task.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)
