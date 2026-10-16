"""D53: catch missing/duplicate DTMF, unauthorized fields and late async effects."""
import asyncio
import shutil
from importlib import import_module, util
from pathlib import Path

import pytest
import yaml
from fakes import FakeJev, jev_choice
from pipecat.frames.frames import OutputAudioRawFrame, OutputDTMFUrgentFrame
from call_engine._support import dtmf_digits, settle, wait_until
from lane_D.agent_support import *


def answer(choice='1', confidence=.9):
    return lambda state, questions, point: {qid: jev_choice(choice, confidence) for qid in questions}


async def boot(h):
    await h.boot()
    assert getattr(h.call, '_keypad', None) is not None, 'live must wire KeypadDecider'


async def turn(h, text='For service, press 1.'):
    await h.sonic.emit_user_text(text)
    await settle(300)


def keypad_decisions(h):
    return [e for e in h.events('decision') if e.policy_decision['intent'] == 'keypad']


async def test_d53_sonic_has_five_tools_and_no_press_keys(agent_call):
    h = agent_call
    await h.boot()
    names = {'update_status', 'ask_user', 'ask_jev', 'request_handoff', 'end_call'}
    assert set(h.sonic.registered_functions) == names
    assert {s.name for s in h.sonic.tools.standard_tools} == names
    assert set(agent_module().TOOL_PARAMETERS) == names


async def test_d53_company_turn_asks_jev_once_and_presses(agent_call):
    h = agent_call
    h.deps.jev = FakeJev([answer(), answer('none')])
    await boot(h)
    await h.sonic_says('I need service.')
    await turn(h)
    assert len(h.deps.jev.calls) == 1
    frames = h.transport.output().frames
    tones = [f for f in frames if isinstance(f, OutputDTMFUrgentFrame)]
    assert len(tones) == 1 and tones[0].to_string() == '1'
    tail = next(f for f in frames[frames.index(tones[0]) + 1:] if isinstance(f, OutputAudioRawFrame))
    assert not any(tail.audio)
    assert len(tail.audio) == h.rate * 2 * h.settings.engine.dtmf.tail_silence_ms // 1000
    d = keypad_decisions(h)[0]
    assert d.decision_point == 'agent_tool'
    assert d.policy_decision == {'intent': 'keypad', 'action': 'press', 'reason_code': 'jev_keypad', 'policy_rule': 'phone-agent/keypad'}
    assert d.action == {'turn_seq': 1}
    assert d.jev_request['questions'] == {k: q.model_dump(mode='json') for k, q in h.deps.jev.calls[0].questions.items()}
    assert d.jev_response == h.deps.jev.calls[0].response.model_dump(mode='json')
    sent = h.events('dtmf_sent')[0]
    assert sent.decision_id == d.decision_id and sent.action == {'digits': '1'} and d.seq < sent.seq
    assert h.event_payloads() == [{'event': 'keys_pressed', 'keys': '1'}]
    await turn(h, 'Please wait.')
    assert [line.text for line in h.deps.jev.calls[-1].state.transcript.lines] == [
        'I need service.', 'For service, press 1.', '[keys pressed: 1]', 'Please wait.']
    assert len(h.transport.output().dtmf()) == 1


@pytest.mark.parametrize('choice,confidence,reason', [('none', .9, 'jev_keypad_none'), ('2', .4, 'jev_keypad_low_confidence')])
async def test_d53_none_and_low_confidence_do_not_press(agent_call, choice, confidence, reason):
    h = agent_call
    h.deps.jev = FakeJev([answer(choice, confidence)])
    await boot(h)
    await turn(h)
    d = keypad_decisions(h)[0]
    assert d.policy_decision['action'] == 'wait' and d.policy_decision['reason_code'] == reason
    assert not h.transport.output().dtmf() and not h.events('dtmf_sent') and not h.event_payloads()


async def test_d53_field_options_follow_authorized_facts(agent_call):
    h = agent_call
    h.spec.compiled.allowed_fields = {'phone_number': '(312) 555-0142'}
    h.deps.jev = FakeJev([answer('none')])
    await boot(h)
    await turn(h)
    call = h.deps.jev.calls[0]
    criteria = next(iter(call.questions.values())).criteria
    assert {'phone_number', 'phone_number#'} <= criteria.keys()
    assert not {'account_number', 'account_number#'} & criteria.keys()
    assert call.state.user_profile == {'phone_number': '(312) 555-0142'}


async def test_d53_field_press_sends_digits_and_pound(agent_call):
    h = agent_call
    h.spec.compiled.allowed_fields = {'phone_number': '(312) 555-0142'}
    h.deps.jev = FakeJev([answer('phone_number#'), answer('none')])
    await boot(h)
    await turn(h, 'Enter your phone number followed by pound.')
    assert dtmf_digits(h.transport.output().dtmf()) == '3125550142#'
    assert h.event_payloads() == [{'event': 'keys_pressed', 'field': 'phone_number'}]
    await turn(h, 'Thank you.')
    transcript = [line.text for line in h.deps.jev.calls[-1].state.transcript.lines]
    assert '[keys pressed: phone_number]' in transcript
    assert '3125550142' not in str(transcript) and '(312)' not in str(transcript)


class DelayedJev(FakeJev):
    def __init__(self):
        super().__init__(default=answer())
        self.entered, self.release = asyncio.Event(), asyncio.Event()
        self.cancelled = False

    async def ask(self, state, questions):
        self.entered.set()
        try:
            await self.release.wait()
            return await super().ask(state, questions)
        except asyncio.CancelledError:
            self.cancelled = True
            raise


async def test_d53_turns_during_a_decision_coalesce(agent_call):
    h = agent_call
    jev = h.deps.jev = DelayedJev()
    await boot(h)
    await turn(h, 'First line')
    assert jev.entered.is_set(), 'transcription must not wait for Jev'
    for text in ['Second line', 'Third line', 'Latest line']:
        await turn(h, text)
    assert len(h.events('company_turn')) == 4 and not jev.calls
    jev.release.set()
    await settle(500)
    assert len(jev.calls) == 2
    assert [d.action['turn_seq'] for d in keypad_decisions(h)] == [1, 4]
    assert jev.calls[-1].state.transcript.lines[-2].text == 'Latest line'
    assert jev.calls[-1].state.transcript.lines[-1].text == '[keys pressed: 1]'


async def test_d53_no_decision_during_handoff(agent_call):
    h = agent_call
    h.deps.jev = FakeJev(default=answer())
    await boot(h)
    await h.call._media_gate.enter_handoff()
    await h.call._keypad.on_company_turn(1)
    await turn(h)
    assert not h.deps.jev.calls and not keypad_decisions(h) and not h.transport.output().dtmf()


@pytest.mark.parametrize('failure', [None, RuntimeError('offline'), TimeoutError('timeout')])
async def test_d53_jev_failure_is_wait(agent_call, failure):
    h = agent_call
    h.deps.jev = None if failure is None else FakeJev([failure])
    await boot(h)
    await turn(h)
    d = keypad_decisions(h)[0]
    assert d.policy_decision['action'] == 'wait'
    assert d.policy_decision['reason_code'] == 'jev_keypad_unavailable'
    assert d.jev_request['state'] and not h.transport.output().dtmf() and not h.event_payloads()


async def test_d53_question_text_comes_from_keypad_yaml(agent_call, tmp_path):
    h = agent_call
    target = tmp_path / 'skills'
    shutil.copytree(h.settings.skills_dir, target)
    path = target / 'phone-agent/references/keypad.yaml'
    cfg = yaml.safe_load(path.read_text())
    cfg['instructions'] = 'Changed skill question'
    cfg['options']['1'] = 'Changed option explanation'
    cfg['min_confidence'] = .95
    path.write_text(yaml.safe_dump(cfg))
    h.settings = h.settings.model_copy(update={'skills_dir': target})
    h.deps.settings = h.settings
    h.deps.jev = FakeJev([answer()])
    await boot(h)
    await turn(h)
    question = next(iter(h.deps.jev.calls[0].questions.values()))
    assert question.instructions == 'Changed skill question'
    assert question.criteria['1'] == 'Changed option explanation'
    assert not h.transport.output().dtmf()


def test_d53_missing_keypad_config_raises(tmp_path):
    assert util.find_spec('waitless.call.keypad') is not None, 'KeypadDecider module missing'
    with pytest.raises(FileNotFoundError):
        import_module('waitless.call.keypad').load_keypad_config(tmp_path)


@pytest.mark.parametrize('ending', ['handoff', 'finish'])
async def test_d53_inflight_reply_cannot_press_after_handoff_or_finish(agent_call, ending):
    h = agent_call
    jev = h.deps.jev = DelayedJev()
    await boot(h)
    await turn(h)
    if ending == 'handoff':
        await h.call._media_gate.enter_handoff()
    else:
        await h.call.hangup('test_cleanup')
    jev.release.set()
    await settle(300)
    assert not h.transport.output().dtmf() and not h.events('dtmf_sent') and not h.event_payloads()
    if ending == 'finish':
        assert jev.cancelled
        await h.call._keypad.on_company_turn(2)
        assert not jev.calls


async def test_d53_transcript_follows_timestamps_when_finals_arrive_late(agent_call):
    from waitless import models as m
    h = agent_call
    h.deps.jev = FakeJev([answer('none')])
    await boot(h)
    h.trace.event(m.TraceEvent(task_id=h.spec.task_id, call_id=h.spec.call_id,
        event_type='company_turn', speaker='company', transcript='Later company line',
        t_ms=30, action={'seq': 1}))
    h.trace.event(m.TraceEvent(task_id=h.spec.task_id, call_id=h.spec.call_id,
        event_type='waitless_utterance', speaker='waitless', transcript='Earlier agent line', t_ms=10))
    await h.call._keypad.on_company_turn(1)
    assert [line.text for line in h.deps.jev.calls[0].state.transcript.lines] == [
        'Earlier agent line', 'Later company line']


async def test_d53_confidence_at_configured_threshold_presses(agent_call):
    h = agent_call
    h.deps.jev = FakeJev([answer('#', .6)])
    await boot(h)
    await turn(h)
    assert dtmf_digits(h.transport.output().dtmf()) == '#'
