"""Ask the skill-owned keypad question; only Jev selects keys and timing."""
import asyncio
from pathlib import Path
import re
import math
import uuid

import yaml
from waitless import models as m
from waitless.call.agent_tools import event_text, send_dtmf
from waitless.decision.jev import build_state, JEV_TRANSCRIPT_MAX_LINES
from waitless.call.speaker_policy import SpeakerPolicyState, apply_guard, classify, valid_answers


def load_keypad_config(skills_dir) -> dict:
    return yaml.safe_load((Path(skills_dir) / 'phone-agent/references/keypad.yaml').read_text(encoding='utf-8'))


SPEAKER_OPTIONS = {'menu_or_recording', 'hold', 'virtual_agent', 'live_human'}


def _load_reference(skills_dir, name, options, *, speaker=False):
    data = yaml.safe_load((Path(skills_dir) / f'phone-agent/references/{name}.yaml').read_text(encoding='utf-8'))
    keys = {'instructions', 'min_confidence', 'options'}
    if speaker:
        keys |= {'automated_max_human_p', 'probability_sum_tolerance'}
    if (not isinstance(data, dict) or set(data) != keys
            or not isinstance(data['instructions'], str) or not data['instructions'].strip()
            or type(data['min_confidence']) not in (int, float)
            or not math.isfinite(data['min_confidence'])
            or not (0 <= data['min_confidence'] <= 1 if speaker else 0 < data['min_confidence'] <= 1)
            or not isinstance(data['options'], dict) or set(data['options']) != options
            or any(not isinstance(v, str) or not v.strip() for v in data['options'].values())):
        raise ValueError(f'invalid {name} configuration')
    if speaker and any(type(data[k]) is not float or not math.isfinite(data[k]) or not 0 < data[k] < 1
            for k in ('automated_max_human_p', 'probability_sum_tolerance')):
        raise ValueError('invalid speaker thresholds')
    return data


def load_speaker_config(skills_dir) -> dict:
    return _load_reference(skills_dir, 'speaker', SPEAKER_OPTIONS, speaker=True)


def load_transfer_config(skills_dir) -> dict:
    return _load_reference(skills_dir, 'transfer', {'none', 'pending', 'connected'})


class KeypadDecider:
    def __init__(self, *, spec, deps, pipeline_task, media_gate, injector, config, speaker_config=None, transfer_config=None, on_speaker=None, on_guard=None):
        self.spec, self.deps = spec, deps
        self.pipeline_task, self.media_gate, self.injector = pipeline_task, media_gate, injector
        self.config = config
        settings = getattr(deps, 'settings', None)
        # State-only consumers (including the compiler's contract checks) do
        # not construct a running call. LiveCall validates this before wiring.
        self.speaker_config = speaker_config
        if self.speaker_config is None and settings is not None:
            self.speaker_config = load_speaker_config(settings.skills_dir)
        self.transfer_config = transfer_config
        if self.transfer_config is None and settings is not None:
            self.transfer_config = load_transfer_config(settings.skills_dir)
        self.on_speaker = on_speaker
        self.on_guard = on_guard
        self.policy_state = SpeakerPolicyState()
        self._last_decided_seq = 0
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

    def _company_turn(self, seq):
        return next((e for e in self._events() if e.event_type == 'company_turn'
            and (e.action or {}).get('seq') == seq), None)

    async def on_company_turn(self, seq: int):
        if self._closed or self.media_gate.in_handoff:
            return
        self.begin_turn(seq)
        if seq < self._latest_seq:
            return
        current = self._company_turn(seq)
        if current is not None and not (current.transcript or '').strip():
            # begin_turn still invalidates in-flight work, but empty FINALs do
            # not ask Jev, consume queued speech, or advance policy counters.
            self._pending_seq = None
            return
        if self._active is not None:
            self._pending_seq = max(seq, self._pending_seq or seq)
            return
        self._active = asyncio.current_task()
        try:
            while (not self._closed and not self.media_gate.in_handoff
                    and self._latest_epoch == self.media_gate.output_epoch):
                self._pending_seq = None
                selected = self._next_seq(seq)
                await self._decide(selected)
                if self._pending_seq is not None:
                    seq = self._pending_seq
                elif selected == seq or seq != self._latest_seq:
                    break
        finally:
            self._active = None
            self._pending_seq = None

    def _next_seq(self, latest):
        # Use exactly the same mixed transcript window as _state, including
        # successful key rows and waitless utterances. Cover any company line
        # that coalescing would otherwise push out of every request.
        entries = self._state_entries(latest)
        covered = {company_seq for _, _, company_seq in entries[-JEV_TRANSCRIPT_MAX_LINES:]}
        for _, _, company_seq in entries:
            if (company_seq is not None and self._last_decided_seq < company_seq <= latest
                    and company_seq not in covered):
                return company_seq
        return latest

    def _events(self):
        return self.deps.trace.events(call_id=self.spec.call_id)

    def _event(self, kind, decision_id, action, **kwargs):
        self.deps.trace.event(m.TraceEvent(task_id=self.spec.task_id, call_id=self.spec.call_id,
            event_type=kind, decision_id=decision_id, action=action,
            t_ms=self.deps.clock.monotonic_ms(), **kwargs))

    def _state_entries(self, seq=None):
        events = self._events()
        turn = self._company_turn(seq)
        cutoff = turn.seq if turn else None
        # Provider FINALs can arrive late; trace seq breaks timestamp ties.
        lines = [((e.t_ms, e.seq), m.TranscriptLine(speaker=e.speaker, text=e.transcript),
                (e.action or {}).get('seq') if e.event_type == 'company_turn' else None)
            for e in events if (cutoff is None or e.seq <= cutoff
                or (e.event_type == 'waitless_utterance' and e.t_ms < turn.t_ms))
            and e.event_type in ('company_turn', 'waitless_utterance') and (e.transcript or '').strip()]
        lines.extend((order, line, None) for origin_seq, order, line in self._pressed
            if seq is None or origin_seq < seq)
        lines.sort(key=lambda item: item[0])
        return lines

    def _state(self, seq=None):
        compiled = self.spec.compiled
        return build_state(task_goal=compiled.task_card.goal, allowed_fields=compiled.allowed_fields,
            standing_rules=self.deps.store.list_standing_rules(),
            prior_choices=[*[m.PriorChoice(question_key=p.question_id, choice=p.selected, source='task_card')
                for p in compiled.task_card.precall_decisions if p.selected is not None],
                *[m.PriorChoice.from_preference(p) for p in self.deps.store.list_preferences(
                    compiled.task_card.company.company_id)]],
            transcript=[line for _, line, _ in self._state_entries(seq)])

    def _notify_guard(self, previous):
        if not previous.guard and self.policy_state.guard and self.on_guard is not None:
            # C wires synchronous revoke_closed + epoch/phase/timers here;
            # this observer must not return an awaitable.
            self.on_guard()

    async def _decide(self, seq):
        epoch = self._latest_epoch
        backfill = seq != self._latest_seq
        if epoch != self.media_gate.output_epoch:
            return
        started = self.deps.clock.monotonic_ms()
        fields = self.spec.compiled.allowed_fields
        field_options = {key: text for key, text in self.config['field_options'].items()
            if key.removesuffix('#') in fields}
        criteria = {**self.config['options'], **field_options}
        questions = {
            'keypad': m.JevQuestion(type='choice', instructions=self.config['instructions'], criteria=criteria),
            'speaker': m.JevQuestion(type='choice', instructions=self.speaker_config['instructions'],
                criteria=self.speaker_config['options']),
            'transfer': m.JevQuestion(type='choice', instructions=self.transfer_config['instructions'],
                criteria=self.transfer_config['options'])}
        state = self._state(seq)
        current = self._company_turn(seq)
        config = {'keypad': {**self.config, 'options': criteria}, 'speaker': self.speaker_config,
            'transfer': self.transfer_config, 'engine': self.deps.settings.engine.speaker,
            'seq': seq, 'evidence': current.transcript if current else ''}
        request = {'state': state.model_dump(mode='json'),
            'questions': {k: q.model_dump(mode='json') for k, q in questions.items()}}
        response, answers = {'answers': {}}, None
        self._last_decided_seq = seq
        try:
            if self.deps.jev is not None and state.transcript.lines:
                reply = await self._ask(state, questions)
                response, answers = reply.model_dump(mode='json'), reply.answers
        except asyncio.CancelledError:
            raise
        except Exception:
            pass
        if self._closed or self.media_gate.in_handoff:
            return
        stale = seq != self._latest_seq or epoch != self.media_gate.output_epoch
        previous = self.policy_state
        valid = valid_answers(answers, config=config)
        if stale:
            if valid:
                self.policy_state = apply_guard(answers, state=previous, config=config)
                self._notify_guard(previous)
            if not backfill:
                # Keep the existing contract for an ordinary in-flight result
                # that became stale: no ordinary decisions or routing effects.
                return
            verdict = None
        else:
            verdict, self.policy_state = classify(answers, state=previous, config=config)
            self._notify_guard(previous)

        who = answers['speaker'] if valid else None
        transfer = answers['transfer'] if valid else None
        policy = {'intent': 'speaker', 'policy_rule': 'phone-agent/speaker',
            'action': verdict.action if verdict else 'wait',
            'reason_code': verdict.reason_code if verdict else 'jev_speaker_stale',
            'speaker_class': verdict.speaker_class if verdict else
                ('guard_wait' if self.policy_state.guard else 'unavailable'),
            'jev_choice': who.choice if who else None,
            'jev_confidence': who.confidence if who else None,
            'p_live_human': who.probabilities['live_human'] if who else None,
            'transfer_choice': transfer.choice if transfer else None,
            'transfer_confidence': transfer.confidence if transfer else None,
            'guard': self.policy_state.guard, 'guard_since_seq': self.policy_state.guard_since_seq,
            'gate_speaker': verdict.gate_speaker if verdict else 'unknown',
            'uncertain_run': self.policy_state.uncertain_run,
            'unavailable_run': self.policy_state.unavailable_run,
            'stale': stale, 'no_jev': False}
        if verdict and verdict.handoff_kind:
            policy['handoff_kind'] = verdict.handoff_kind
        speaker_id = uuid.uuid4().hex
        self._event('decision', speaker_id, {'turn_seq': seq}, decision_point='agent_tool',
            jev_request=request, jev_response=response, policy_decision=policy,
            latency_ms=self.deps.clock.monotonic_ms() - started)
        choice = None
        if verdict is None or verdict.speaker_class != 'automated':
            reason = ('jev_keypad_speaker_handoff' if verdict and verdict.action == 'handoff'
                else 'jev_keypad_speaker_closed')
        else:
            keypad = answers['keypad']
            if keypad.choice == 'none':
                reason = 'jev_keypad_none'
            elif keypad.confidence < self.config['min_confidence']:
                reason = 'jev_keypad_low_confidence'
            else:
                choice, reason = keypad.choice, 'jev_keypad'
        decision_id = uuid.uuid4().hex
        self._event('decision', decision_id, {'turn_seq': seq}, decision_point='agent_tool',
            jev_request=request, jev_response=response,
            policy_decision={'intent': 'keypad', 'action': 'press' if choice is not None else 'wait',
                'reason_code': reason, 'policy_rule': 'phone-agent/keypad'},
            latency_ms=self.deps.clock.monotonic_ms() - started)
        if stale:
            return
        if self.on_speaker is not None:
            await self.on_speaker(seq, verdict, speaker_id)
        else:
            await self.media_gate.resolve_speaker(seq, verdict.gate_speaker)
        valid_epoch = (epoch == self.media_gate.output_epoch or (verdict.gate_speaker == 'hold'
            and self.media_gate.output_state == 'hold' and self.media_gate.output_epoch == epoch + 1))
        if (choice is None or self._closed or self.media_gate.in_handoff or seq != self._latest_seq
                or not valid_epoch or self.policy_state.guard):
            return
        field = choice.removesuffix('#') if choice in field_options else None
        digits = (re.sub(r'\D', '', fields[field]) + ('#' if choice.endswith('#') else '')) if field else choice
        task = self.pipeline_task()
        await send_dtmf(task, self.deps.settings, digits, output_epoch=self.media_gate.output_epoch)
        self._event('dtmf_sent', decision_id, {'digits': digits, 'keys': len(digits)})
        sent = self._events()[-1]
        self._pressed.append((seq, (sent.t_ms, sent.seq), m.TranscriptLine(speaker='waitless',
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
