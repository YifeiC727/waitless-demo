"""Phone-agent tool boundaries: validate arguments and carry out AI-requested effects."""
import asyncio
import json
import re
import uuid
from datetime import datetime

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

TOOL_NAMES = ('press_keys', 'update_status', 'ask_user', 'ask_jev', 'request_handoff', 'end_call')
AGENT_EVENT_PREFIX = '[Waitless event] '
AGENT_PHASES = ('ivr', 'hold', 'ai_agent', 'live_rep', 'wrapping_up')


def _object(properties, required):
    return {'type': 'object', 'properties': properties, 'required': required}


TOOL_PARAMETERS = {
    'press_keys': _object({'digits': {'type': 'string', 'pattern': '^[0-9*#]{1,32}$'}}, ['digits']),
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


class AgentToolbox:
    def __init__(self, *, spec, deps, pipeline_task, media_gate, monitor, injector, carrier, call_sid):
        self.spec, self.deps = spec, deps
        self.pipeline_task, self.media_gate, self.monitor = pipeline_task, media_gate, monitor
        self.injector, self.carrier, self.call_sid = injector, carrier, call_sid
        self.phase = 'dialing'
        self.status_line = None
        self.action_phase = None
        self.handoff_decision_id = None
        self.prior_answers = []
        self._tasks = set()
        self._budget_lock = asyncio.Lock()
        self._notified_choices = set()
        self._ending = False
        self._closed = False
        self._handoff_dropped_start = 0

    @property
    def interrupts_used(self):
        return sum(e.event_type in ('pending_choice_created', 'handoff_ready') for e in self._events())

    def schema(self) -> ToolsSchema:
        return ToolsSchema(standard_tools=[FunctionSchema(name=name,
            description=self.spec.compiled.agent_tool_descriptions.get(name, ''),
            properties=TOOL_PARAMETERS[name]['properties'], required=TOOL_PARAMETERS[name]['required'])
            for name in TOOL_NAMES])

    def register(self, llm):
        for name in TOOL_NAMES:
            async def handler(params: FunctionCallParams, name=name):
                await self._invoke(name, params)
            llm.register_function(name, handler)

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
            policy_decision={'intent': name, 'action': {'press_keys': 'press', 'ask_user': 'ask_user',
                'ask_jev': 'wait', 'request_handoff': 'handoff', 'end_call': 'end'}[name],
                'reason_code': 'agent_tool', 'policy_rule': 'phone-agent'},
            latency_ms=self.deps.clock.monotonic_ms() - started)
        return decision_id

    async def _invoke(self, name: str, params: FunctionCallParams):
        args, started = dict(params.arguments), self.deps.clock.monotonic_ms()
        self._event('agent_tool_call', action={'tool': name, 'tool_call_id': params.tool_call_id, 'args': args})
        cancelled = None
        try:
            if not _valid(args, TOOL_PARAMETERS[name]):
                code = 'invalid_digits' if name == 'press_keys' else (
                    'invalid_phase' if name == 'update_status' and args.get('phase') not in AGENT_PHASES
                    else 'invalid_arguments')
                result = {'ok': False, 'error': code}
            elif self._closed:
                result = {'ok': False, 'error': 'call_ended'}
            else:
                result = await getattr(self, '_' + name)(args, started)
        except asyncio.CancelledError as exc:
            cancelled = exc
            result = {'ok': False, 'error': 'cancelled'}
        except Exception:
            # Tool failures are data for the agent; never expose transport or validation details.
            result = {'ok': False, 'error': 'tool_failed'}
        self._event('agent_tool_result', action={'tool': name, 'tool_call_id': params.tool_call_id,
            'ok': result.get('ok', True), 'result': result,
            'latency_ms': self.deps.clock.monotonic_ms() - started})
        if cancelled is not None:
            raise cancelled
        await params.result_callback(result)

    def _budget_error(self):
        budget = self.spec.compiled.policy['global']['interrupt_budget']
        used = self.interrupts_used
        if used >= budget:
            return {'ok': False, 'error': 'interrupt_budget_exhausted', 'used': used, 'budget': budget}
        return None

    async def _press_keys(self, args, started):
        if self.media_gate.in_handoff:
            return {'ok': False, 'error': 'line_in_handoff'}
        decision_id = self._decision('press_keys', args, started)
        await self.pipeline_task.queue_frame(OutputDTMFUrgentFrame.from_string(args['digits']))
        engine = self.deps.settings.engine
        audio = silence_pcm16(engine.dtmf.tail_silence_ms, engine.audio.pipeline_in_sample_rate)
        if audio:
            tail = OutputAudioRawFrame(audio=audio, sample_rate=engine.audio.pipeline_in_sample_rate, num_channels=1)
            tail._waitless_dtmf_tail = True
            await self.pipeline_task.queue_frame(tail)
        self._event('dtmf_sent', decision_id, {'digits': args['digits']})
        return {'ok': True, 'sent': args['digits']}

    async def _update_status(self, args, started):
        self.phase, self.status_line = args['phase'], args['status_line']
        # Resuming lasts until the agent reports its current stage.
        if self.action_phase == 'resuming':
            self.action_phase = None
        self._event('status_updated', action=args)
        return {'ok': True}

    async def _ask_user(self, args, started):
        async with self._budget_lock:
            if self.deps.pending.current(self.spec.call_id) is not None:
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
        current = self.deps.pending.get(choice.choice_id)
        if current is not None and current.status in ('open', 'expired'):
            self.deps.pending.expire_due()
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
            if self.media_gate.in_handoff:
                return {'ok': False, 'error': 'handoff_already_active'}
            error = self._budget_error()
            if error:
                return error
            await self.monitor.wait_idle(self.deps.settings.engine.handoff.announce_idle_ms)
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
            self._event('handoff_started', decision_id)
            return {'status': 'handoff_ready'}

    async def on_handoff_returned(self):
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
        self._closed = True
        tasks = [t for t in self._tasks if t is not asyncio.current_task()]
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
