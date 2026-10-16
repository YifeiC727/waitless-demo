"""Ask the skill-owned keypad question; only Jev selects keys and timing."""
import asyncio
from pathlib import Path
import re
import uuid

import yaml
from waitless import models as m
from waitless.call.agent_tools import event_text, send_dtmf
from waitless.decision.jev import build_state


def load_keypad_config(skills_dir) -> dict:
    return yaml.safe_load((Path(skills_dir) / 'phone-agent/references/keypad.yaml').read_text(encoding='utf-8'))


class KeypadDecider:
    def __init__(self, *, spec, deps, pipeline_task, media_gate, injector, config):
        self.spec, self.deps = spec, deps
        self.pipeline_task, self.media_gate, self.injector = pipeline_task, media_gate, injector
        self.config = config
        self._closed = False
        self._active = None
        self._pending_seq = None
        self._pressed = []

    async def on_company_turn(self, seq: int):
        if self._closed or self.media_gate.in_handoff:
            return
        if self._active is not None:
            self._pending_seq = seq
            return
        self._active = asyncio.current_task()
        try:
            while not self._closed and not self.media_gate.in_handoff:
                self._pending_seq = None
                await self._decide(seq)
                if self._pending_seq is None:
                    break
                seq = self._pending_seq
        finally:
            self._active = None
            self._pending_seq = None

    def _events(self):
        return self.deps.trace.events(call_id=self.spec.call_id)

    def _event(self, kind, decision_id, action, **kwargs):
        self.deps.trace.event(m.TraceEvent(task_id=self.spec.task_id, call_id=self.spec.call_id,
            event_type=kind, decision_id=decision_id, action=action,
            t_ms=self.deps.clock.monotonic_ms(), **kwargs))

    def _state(self):
        # Provider FINALs can arrive late; seq breaks ties in their timestamps.
        lines = [((e.t_ms, e.seq), m.TranscriptLine(speaker=e.speaker, text=e.transcript))
            for e in self._events() if e.event_type in ('company_turn', 'waitless_utterance') and e.transcript]
        lines.extend(self._pressed)
        lines.sort(key=lambda item: item[0])
        compiled = self.spec.compiled
        return build_state(task_goal=compiled.task_card.goal, allowed_fields=compiled.allowed_fields,
            standing_rules=self.deps.store.list_standing_rules(),
            prior_choices=[*[m.PriorChoice(question_key=p.question_id, choice=p.selected, source='task_card')
                for p in compiled.task_card.precall_decisions if p.selected is not None],
                *[m.PriorChoice.from_preference(p) for p in self.deps.store.list_preferences(
                    compiled.task_card.company.company_id)]],
            transcript=[line for _, line in lines])

    async def _decide(self, seq):
        started = self.deps.clock.monotonic_ms()
        fields = self.spec.compiled.allowed_fields
        field_options = {key: text for key, text in self.config['field_options'].items()
            if key.removesuffix('#') in fields}
        criteria = {**self.config['options'], **field_options}
        question = m.JevQuestion(type='choice', instructions=self.config['instructions'], criteria=criteria)
        state = self._state()
        request = {'state': state.model_dump(mode='json'), 'questions': {'keypad': question.model_dump(mode='json')}}
        response = {'answers': {}}
        choice, reason = None, 'jev_keypad_unavailable'
        try:
            if self.deps.jev is None:
                raise RuntimeError('unavailable')
            reply = await self.deps.jev.ask(state, {'keypad': question})
            response = reply.model_dump(mode='json')
            answer = reply.answers['keypad']
            if answer.type != 'choice' or answer.choice not in criteria:
                raise ValueError('invalid choice')
            if answer.choice == 'none':
                reason = 'jev_keypad_none'
            elif answer.confidence < self.config['min_confidence']:
                reason = 'jev_keypad_low_confidence'
            else:
                choice, reason = answer.choice, 'jev_keypad'
        except Exception:
            choice, reason = None, 'jev_keypad_unavailable'
        # A reply can outlive the call or the transition to user media.
        if self._closed or self.media_gate.in_handoff:
            return
        decision_id = uuid.uuid4().hex
        self._event('decision', decision_id, {'turn_seq': seq}, decision_point='agent_tool',
            jev_request=request, jev_response=response,
            policy_decision={'intent': 'keypad', 'action': 'press' if choice is not None else 'wait',
                'reason_code': reason, 'policy_rule': 'phone-agent/keypad'},
            latency_ms=self.deps.clock.monotonic_ms() - started)
        if choice is None:
            return
        field = choice.removesuffix('#') if choice in field_options else None
        digits = (re.sub(r'\D', '', fields[field]) + ('#' if choice.endswith('#') else '')) if field else choice
        task = self.pipeline_task()
        await send_dtmf(task, self.deps.settings, digits)
        self._event('dtmf_sent', decision_id, {'digits': digits})
        sent = self._events()[-1]
        self._pressed.append(((sent.t_ms, sent.seq), m.TranscriptLine(speaker='waitless',
            text=f'[keys pressed: {field if field else choice}]')))
        await self.injector.send_event(event_text({'event': 'keys_pressed', **({'field': field} if field else {'keys': choice})}))

    async def close(self):
        self._closed = True
        task = self._active
        if task is not None and task is not asyncio.current_task():
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
