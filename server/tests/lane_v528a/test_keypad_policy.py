"""T4: real LiveCall/MediaGate, offline Jev and line replacements only."""
import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock
import pytest
from fakes import FakeJev, jev_choice
from call_engine._support import settle
from lane_D.agent_support import agent_call, settings, spec, engine, compiled, line_number, store, trace
from waitless import models as m
from lane_v528a.test_policy import answers


def script(who='virtual_agent',key='1',confidence=.95,transfer='none',tc=.9):
    return {'speaker':jev_choice(who,confidence),'keypad':jev_choice(key),
            'transfer':jev_choice(transfer,tc)}

@pytest.fixture
async def live528(agent_call):
    h=agent_call;h.component_tools=False;h.deps.jev=FakeJev(default=script())
    await h.boot()
    return h

async def turn(h,text='Company line'):
    await h.sonic.emit_user_text(text);await settle(300)

def speaker_decisions(h):
    return [e for e in h.events('decision') if e.policy_decision['intent']=='speaker']

def keypad_decisions(h):
    return [e for e in h.events('decision') if e.policy_decision['intent']=='keypad']

async def test_three_questions_guard_suppresses_keys_and_trace(live528):
    h=live528;h.call._keypad.deps.jev=FakeJev(default=script(transfer='pending'))
    await turn(h)
    d=speaker_decisions(h)[-1];p=d.policy_decision
    assert set(d.jev_request['questions'])=={'keypad','speaker','transfer'}
    assert p['speaker_class']=='guard_wait' and p['guard'] and p['guard_since_seq']==1
    assert p['jev_choice']=='virtual_agent' and p['jev_confidence']==.95
    assert p['p_live_human']<.2 and p['transfer_choice']=='pending' and p['transfer_confidence']==.9
    assert p['gate_speaker']=='unknown' and p['uncertain_run']==p['unavailable_run']==0
    assert p['stale'] is False and p['no_jev'] is False
    assert h.call.status().phase=='hold' and h.call._media_gate.output_state=='closed'
    assert not h.transport.output().dtmf()
    assert keypad_decisions(h)[-1].policy_decision['reason_code']=='jev_keypad_speaker_closed'
    assert h.events('status_updated')[-1].action['speaker_class']=='guard_wait'
    assert all(e.speaker in (None,'company','waitless','user','system') for e in h.events())

@pytest.mark.parametrize('transfer',['connected'])
async def test_g1_same_package_handoffs_and_suppresses_keys(live528,transfer):
    h=live528;h.call._keypad.deps.jev=FakeJev(default=script(transfer=transfer))
    await turn(h)
    p=speaker_decisions(h)[-1].policy_decision
    assert p['action']=='handoff' and p['handoff_kind']=='transfer_connected' and p['guard']
    assert h.call._media_gate.in_handoff and not h.transport.output().dtmf()
    assert keypad_decisions(h)[-1].policy_decision['reason_code']=='jev_keypad_speaker_handoff'

@pytest.mark.parametrize('count,expected',[(8,[1,9]),(11,[1,2,3,4,12])])
async def test_queue_covers_every_company_turn(live528,count,expected):
    h=live528;release=asyncio.Event();entered=asyncio.Event();snapshots=[]
    async def ask(state,questions):
        snapshots.append(state)
        if len(snapshots)==1:entered.set();await release.wait()
        # The announcement at seq2 is deliberately absent from latest's window.
        transfer='pending' if state.transcript.lines[-1].text=='line 2' else 'none'
        a=answers(transfer=transfer);a['keypad'].choice='none';a['keypad'].probabilities={'none':.9}
        return m.JevResponse(answers=a)
    h.call._keypad.deps.jev=SimpleNamespace(ask=ask)
    await turn(h,'line 1');assert entered.is_set()
    for n in range(2,count+2):await turn(h,f'line {n}')
    release.set();await settle(1500)
    assert [int(s.transcript.lines[-1].text.split()[-1]) for s in snapshots]==expected
    assert {line.text for state in snapshots for line in state.transcript.lines} >= {f'line {n}' for n in range(1,count+2)}
    if count==11:
        stale=[d for d in speaker_decisions(h) if d.policy_decision['stale']]
        assert [d.action['turn_seq'] for d in stale]==[2,3,4]
        assert h.call._keypad.policy_state.guard
        assert h.call._keypad.policy_state.uncertain_run==h.call._keypad.policy_state.unavailable_run==0
    assert not h.events('dtmf_sent')

async def test_backlog_before_first_decider_starts_is_not_skipped(live528):
    h=live528;g=h.call._media_gate;k=h.call._keypad;snapshots=[]
    async def ask(state,questions):snapshots.append(state);return m.JevResponse(answers=answers(transfer='pending'))
    k.deps.jev=SimpleNamespace(ask=ask)
    for n in range(1,13):
        h.trace.event(m.TraceEvent(task_id=h.spec.task_id,call_id=h.spec.call_id,event_type='company_turn',
            speaker='company',transcript=f'line {n}',action={'seq':n,'kind':'speech'},t_ms=h.clock.monotonic_ms()))
        g.begin_pending(n);k.begin_turn(n)
    await g.clear_output()
    await k.on_company_turn(12)
    assert [int(s.transcript.lines[-1].text.split()[-1]) for s in snapshots]==[1,2,3,4,12]
    assert h.call._keypad.policy_state.guard and not h.events('dtmf_sent')

@pytest.mark.parametrize('stale_by',['seq','epoch'])
async def test_stale_only_sets_guard_never_speaker_or_keys(live528,stale_by):
    h=live528;release=asyncio.Event();entered=asyncio.Event()
    async def ask(state,questions):
        entered.set();await release.wait()
        return m.JevResponse(answers=answers('live_human',1,transfer='pending'))
    k=h.call._keypad;k.deps.jev=SimpleNamespace(ask=ask)
    await turn(h);assert entered.is_set()
    if stale_by=='seq':await turn(h,'   ')
    else:await h.call._media_gate.invalidate_stream()
    release.set();await settle(500)
    assert k.policy_state.guard and k.policy_state.guard_since_seq==1
    assert k.policy_state.uncertain_run==k.policy_state.unavailable_run==0
    decisions = h.events('decision')
    assert ([d.policy_decision['speaker_class'] for d in decisions] == ['empty']
            if stale_by == 'seq' else not decisions)
    assert all(d.policy_decision['no_jev'] for d in decisions)
    assert not h.events('dtmf_sent') and not h.events('handoff_ready')

async def test_key_rows_belong_to_decision_seq_at_same_millisecond(live528):
    h=live528;h.call._keypad.deps.jev=FakeJev([script(key='*'),script(key='none')])
    await turn(h,'Menu offers representative on star.')
    k=h.call._keypad
    assert not any(line.text.startswith('[keys pressed:') for line in k._state(1).transcript.lines)
    await turn(h,'Representative greeting.')
    assert [line.text for line in k._state(2).transcript.lines]==[
        'Menu offers representative on star.','[keys pressed: *]','Representative greeting.']
    assert len({e.t_ms for e in h.events('company_turn')+h.events('dtmf_sent')})==1

async def test_send_failure_does_not_add_key_row(live528,monkeypatch):
    h=live528
    from waitless.call import keypad
    send = AsyncMock(side_effect=RuntimeError('queue failed'))
    monkeypatch.setattr(keypad,'send_dtmf',send)
    await turn(h)
    send.assert_awaited_once()
    assert not h.events('dtmf_sent')
    assert not any(line.text.startswith('[keys pressed:') for line in h.call._keypad._state().transcript.lines)

async def test_empty_only_begins_turn_no_jev_or_counters(live528):
    h=live528;k=h.call._keypad
    await turn(h,'   ')
    assert k._latest_seq==1 and not h.deps.jev.calls
    assert [d.policy_decision['speaker_class'] for d in h.events('decision')]==['empty']
    assert h.events('decision')[0].policy_decision['no_jev'] is True
    assert h.call._media_gate.output_state=='closed'
    assert k.policy_state.uncertain_run==k.policy_state.unavailable_run==0
    assert not k._state().transcript.lines

async def test_uncertain_unavailable_pause_and_r2_restore(live528):
    h=live528;k=h.call._keypad
    async def ask(*_):return m.JevResponse(answers=answers(ph=.2))
    k.deps.jev=SimpleNamespace(ask=ask)
    await turn(h)
    assert k.policy_state.uncertain_run==1 and h.call._media_gate.output_state=='closed'
    assert h.call.status().phase=='dialing' and not h.events('dtmf_sent')
    k.deps.jev=None;await turn(h)
    assert k.policy_state.uncertain_run==1 and k.policy_state.unavailable_run==1
    await turn(h,' ')
    assert k.policy_state.uncertain_run==1 and k.policy_state.unavailable_run==1
    k.deps.jev=FakeJev(default=script(key='none'));await turn(h)
    assert k.policy_state.uncertain_run==k.policy_state.unavailable_run==0
    assert h.call._media_gate.output_state=='allowed' and h.call.status().phase=='ai_agent'
    assert ' ' not in [line.text for line in k._state().transcript.lines]

async def test_guard_observer_is_synchronous_before_speaker_callback(live528):
    h=live528;k=h.call._keypad;order=[]
    k.on_guard=lambda:order.append(('guard',k.policy_state.guard))
    original=k.on_speaker
    async def callback(seq,verdict,decision_id):
        order.append(('speaker',verdict.speaker_class));await original(seq,verdict,decision_id)
    k.on_speaker=callback;k.deps.jev=FakeJev(default=script(transfer='pending'))
    await turn(h)
    assert order==[('guard',True),('speaker','guard_wait')]

async def test_epoch_swap_before_backfill_does_not_spin_or_reuse_old_snapshot(live528, monkeypatch):
    h=live528;g=h.call._media_gate;k=h.call._keypad
    for n in range(1,13):
        h.trace.event(m.TraceEvent(task_id=h.spec.task_id,call_id=h.spec.call_id,event_type='company_turn',
            speaker='company',transcript=f'line {n}',action={'seq':n,'kind':'speech'},t_ms=h.clock.monotonic_ms()))
        g.begin_pending(n);k.begin_turn(n)
    await g.clear_output()
    await g.invalidate_stream()
    choose=k._next_seq;attempts=[]
    def bounded_choice(seq):
        attempts.append(seq)
        # Fail deterministically instead of letting an epoch mismatch spin
        # forever and block the event loop (and the test timeout itself).
        assert len(attempts)<=1, 'stale epoch repeated the same backfill without progress'
        return choose(seq)
    monkeypatch.setattr(k,'_next_seq',bounded_choice)
    await k.on_company_turn(12)
    assert not h.deps.jev.calls and not h.events('decision')

async def test_backfill_uses_mixed_eight_line_window(live528):
    h=live528;g=h.call._media_gate;k=h.call._keypad;snapshots=[]
    async def ask(state,questions):
        snapshots.append(state)
        return m.JevResponse(answers=answers(transfer='pending'))
    k.deps.jev=SimpleNamespace(ask=ask)
    for n in range(1,13):
        for kind,speaker,text,action in [('company_turn','company',f'line {n}',{'seq':n,'kind':'speech'}),
                ('waitless_utterance','waitless',f'agent {n}',{})]:
            h.trace.event(m.TraceEvent(task_id=h.spec.task_id,call_id=h.spec.call_id,event_type=kind,
                speaker=speaker,transcript=text,action=action,t_ms=h.clock.monotonic_ms()))
        g.begin_pending(n);k.begin_turn(n)
    await g.clear_output();await k.on_company_turn(12)
    assert {line.text for state in snapshots for line in state.transcript.lines if line.speaker=='company'} == {f'line {n}' for n in range(1,13)}
    assert all(len(state.transcript.lines)<=8 for state in snapshots)
    assert not h.events('dtmf_sent') and k.policy_state.guard

@pytest.mark.parametrize('kind,reason', [('uncertain','speaker_uncertain_repeated'),
    ('unavailable','speaker_unavailable_repeated')])
async def test_three_consecutive_results_handoff_without_changing_phase(live528,kind,reason):
    h=live528;k=h.call._keypad
    async def ask(*_):return m.JevResponse(answers=answers(ph=.2))
    k.deps.jev=SimpleNamespace(ask=ask) if kind=='uncertain' else None
    for _ in range(2):
        await turn(h)
        assert not h.events('handoff_ready') and not h.transport.output().dtmf()
    assert h.call._toolbox.phase=='dialing'
    await turn(h)
    p=speaker_decisions(h)[-1].policy_decision
    assert p['action']=='handoff' and p['handoff_kind']=='needs_user' and p['reason_code']==reason
    assert h.call._toolbox.phase=='dialing' and len(h.events('handoff_ready'))==1
    assert not h.transport.output().dtmf()
