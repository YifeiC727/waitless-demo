"""Ask the skill-owned keypad question; only Jev selects keys and timing."""
import asyncio
from pathlib import Path
import re
import math
import uuid

import yaml
from waitless import models as m
from waitless.call.agent_tools import event_text, send_dtmf
from waitless.decision.jev import build_state


def load_keypad_config(skills_dir) -> dict:
    return yaml.safe_load((Path(skills_dir) / 'phone-agent/references/keypad.yaml').read_text(encoding='utf-8'))


SPEAKER_OPTIONS = {'menu_or_recording', 'hold', 'virtual_agent', 'live_human'}


def load_speaker_config(skills_dir) -> dict:
    data = yaml.safe_load((Path(skills_dir) / 'phone-agent/references/speaker.yaml').read_text(encoding='utf-8'))
    if (not isinstance(data, dict) or not {'instructions', 'min_confidence', 'options'} <= data.keys()
            or not isinstance(data['instructions'], str) or not data['instructions'].strip()
            or type(data['min_confidence']) not in (int, float)
            or not 0 <= data['min_confidence'] <= 1
            or not isinstance(data['options'], dict) or set(data['options']) != SPEAKER_OPTIONS
            or any(not isinstance(v, str) or not v.strip() for v in data['options'].values())):
        raise ValueError('invalid speaker configuration')
    return data


class KeypadDecider:
    def __init__(self, *, spec, deps, pipeline_task, media_gate, injector, config, speaker_config=None, on_speaker=None):
        self.spec, self.deps = spec, deps
        self.pipeline_task, self.media_gate, self.injector = pipeline_task, media_gate, injector
        self.config = config
        settings = getattr(deps, 'settings', None)
        # State-only consumers (including the compiler's contract checks) do
        # not construct a running call. LiveCall validates this before wiring.
        self.speaker_config = speaker_config
        if self.speaker_config is None and settings is not None:
            self.speaker_config = load_speaker_config(settings.skills_dir)
        self.on_speaker = on_speaker
        self._latest_seq = 0
        self._latest_epoch = getattr(media_gate, 'output_epoch', 0)
        self._closed = False
        self._active = None
        self._pending_seq = None
        self._pressed = []

    def begin_turn(self, seq):
        if seq > self._latest_seq:
            self._latest_seq = seq
            self._latest_epoch = self.media_gate.output_epoch

    async def on_company_turn(self, seq: int):
        if self._closed or self.media_gate.in_handoff:
            return
        self.begin_turn(seq)
        if seq < self._latest_seq:
            return
        if self._active is not None:
            self._pending_seq = max(seq, self._pending_seq or seq)
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

    def _state(self, seq=None):
        events = self._events()
        turn = next((e for e in events if e.event_type == 'company_turn' and (e.action or {}).get('seq') == seq), None)
        cutoff = turn.seq if turn else None
        # Provider FINALs can arrive late; seq breaks ties in their timestamps.
        lines = [((e.t_ms, e.seq), m.TranscriptLine(speaker=e.speaker, text=e.transcript))
            for e in events if (cutoff is None or e.seq <= cutoff
                or (e.event_type == 'waitless_utterance' and e.t_ms < turn.t_ms))
            and e.event_type in ('company_turn', 'waitless_utterance') and e.transcript]
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
        epoch = self._latest_epoch
        if seq != self._latest_seq or epoch != self.media_gate.output_epoch:
            return
        started = self.deps.clock.monotonic_ms()
        fields = self.spec.compiled.allowed_fields
        field_options = {key: text for key, text in self.config['field_options'].items()
            if key.removesuffix('#') in fields}
        criteria = {**self.config['options'], **field_options}
        question = m.JevQuestion(type='choice', instructions=self.config['instructions'], criteria=criteria)
        speaker_question = m.JevQuestion(type='choice', instructions=self.speaker_config['instructions'],
            criteria=self.speaker_config['options'])
        questions = {'keypad': question, 'speaker': speaker_question}
        state = self._state(seq)
        request = {'state': state.model_dump(mode='json'),
            'questions': {k: q.model_dump(mode='json') for k, q in questions.items()}}
        response = {'answers': {}}
        choice, reason, speaker = None, 'jev_keypad_unavailable', 'unknown'
        speaker_reason = 'jev_speaker_unavailable'
        try:
            current = next((e for e in self._events() if e.event_type == 'company_turn' and (e.action or {}).get('seq') == seq), None)
            if (self.deps.jev is None or not state.transcript.lines
                    or (current is not None and not (current.transcript or '').strip())):
                raise RuntimeError('unavailable or empty transcript')
            reply = await self._ask(state, questions)
            response = reply.model_dump(mode='json')
            if set(reply.answers) != set(questions):
                raise ValueError('incomplete answer package')
            for key, answer in reply.answers.items():
                if (answer.type != 'choice' or answer.choice not in questions[key].criteria
                        or type(answer.confidence) not in (int, float) or not math.isfinite(answer.confidence)
                        or not 0 <= answer.confidence <= 1):
                    raise ValueError('invalid choice package')
            keypad, who = reply.answers['keypad'], reply.answers['speaker']
            if keypad.choice == 'none':
                reason = 'jev_keypad_none'
            elif keypad.confidence < self.config['min_confidence']:
                reason = 'jev_keypad_low_confidence'
            else:
                choice, reason = keypad.choice, 'jev_keypad'
            if who.confidence < self.speaker_config['min_confidence']:
                speaker_reason = 'jev_speaker_low_confidence'
            else:
                speaker, speaker_reason = who.choice, 'jev_speaker'
        except asyncio.CancelledError:
            raise
        except Exception:
            choice, reason = None, 'jev_keypad_unavailable'
        # Results are tied to the snapshot, never to the current global trace.
        if (self._closed or self.media_gate.in_handoff or seq != self._latest_seq
                or epoch != self.media_gate.output_epoch):
            return
        speaker_id = uuid.uuid4().hex
        self._event('decision', speaker_id, {'turn_seq': seq}, decision_point='agent_tool',
            jev_request=request, jev_response=response,
            policy_decision={'intent': 'speaker', 'action': 'handoff' if speaker in ('live_human', 'unknown') else 'wait',
                'reason_code': speaker_reason, 'policy_rule': 'phone-agent/speaker'},
            latency_ms=self.deps.clock.monotonic_ms() - started)
        if speaker in ('live_human', 'unknown') and choice is not None:
            choice, reason = None, 'jev_keypad_speaker_handoff'
        decision_id = uuid.uuid4().hex
        self._event('decision', decision_id, {'turn_seq': seq}, decision_point='agent_tool',
            jev_request=request, jev_response=response,
            policy_decision={'intent': 'keypad', 'action': 'press' if choice is not None else 'wait',
                'reason_code': reason, 'policy_rule': 'phone-agent/keypad'},
            latency_ms=self.deps.clock.monotonic_ms() - started)
        if self.on_speaker is not None:
            await self.on_speaker(seq, speaker, speaker_id)
        else:
            await self.media_gate.resolve_speaker(seq, speaker)
        valid_epoch = (epoch == self.media_gate.output_epoch or (speaker == 'hold'
            and self.media_gate.output_state == 'hold' and self.media_gate.output_epoch == epoch + 1))
        if (choice is None or self._closed or self.media_gate.in_handoff or seq != self._latest_seq
                or not valid_epoch):
            return
        field = choice.removesuffix('#') if choice in field_options else None
        digits = (re.sub(r'\D', '', fields[field]) + ('#' if choice.endswith('#') else '')) if field else choice
        task = self.pipeline_task()
        await send_dtmf(task, self.deps.settings, digits, output_epoch=self.media_gate.output_epoch)
        self._event('dtmf_sent', decision_id, {'digits': digits, 'keys': len(digits)})
        sent = self._events()[-1]
        self._pressed.append(((sent.t_ms, sent.seq), m.TranscriptLine(speaker='waitless',
            text=f'[keys pressed: {field if field else choice}]')))
        await self.injector.send_event(event_text({'event': 'keys_pressed', **({'field': field} if field else {'keys': choice})}))

    async def _ask(self, state, questions):
        ask = asyncio.create_task(self.deps.jev.ask(state, questions))
        timer = asyncio.create_task(self.deps.clock.sleep(self.deps.settings.engine.speaker.verdict_timeout_ms / 1000))
        try:
            done, _ = await asyncio.wait((ask, timer), return_when=asyncio.FIRST_COMPLETED)
            if timer in done:
                raise TimeoutError('speaker verdict timeout')
            return await ask
        finally:
            for task in (ask, timer):
                if not task.done():
                    task.cancel()
            await asyncio.gather(ask, timer, return_exceptions=True)

    async def close(self):
        self._closed = True
        task = self._active
        if task is not None and task is not asyncio.current_task():
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
