"""C2 catches missing LiveCall routing, FINAL marks, quiet rechecks and timers.

Real LiveCall, MediaGateIn/Out and Pipecat pipeline; only Jev/line/provider
are offline doubles. Audio uses frame arrival times via the actual inbound.
"""
import asyncio
from dataclasses import replace
from types import SimpleNamespace
import pytest
from fakes import FakeJev
from waitless import models as m
from call_engine._support import settle, zero_pcm16, in_frame
from lane_v528a.test_policy import answers
from lane_v528a.test_keypad_policy import (live528, agent_call, settings, spec,
    engine, compiled, line_number, store, trace, turn, script, speaker_decisions)


def reply(h, **kwargs):
    async def ask(state, questions):
        a=answers(**kwargs)
        a['keypad'].choice='none'; a['keypad'].probabilities={'none':1.0}
        return m.JevResponse(answers={k: v for k, v in (a).items() if k in questions})
    h.call._keypad.deps.jev = SimpleNamespace(ask=ask)


def fallback(h):
    return [e for e in h.events('decision') if e.policy_decision['intent']=='speaker_fallback']


def assert_ready(h, kind, reason=None):
    assert len(h.events('handoff_ready')) == 1
    assert h.call.status().handoff.reason_kind == kind
    if reason:
        assert fallback(h)[-1].policy_decision['reason_code'] == reason
    assert not h.transport.output().dtmf()


@pytest.mark.parametrize('kind,who,transfer', [
    ('live_human','live_human','none'),
    ('transfer_connected','virtual_agent','connected')])
async def test_normal_verdict_preserves_kind(live528,kind,who,transfer):
    h=live528; reply(h,who=who,confidence=.9,transfer=transfer)
    await turn(h)
    assert_ready(h,kind)
    assert h.call._toolbox.phase == 'live_rep'


@pytest.mark.parametrize('mode,reason', [('uncertain','speaker_uncertain_repeated'),
    ('unavailable','speaker_unavailable_repeated')])
async def test_repeated_caps_use_needs_user_without_live_rep(live528,mode,reason):
    h=live528
    if mode=='uncertain': reply(h,ph=.2)
    else: h.call._keypad.deps.jev=None
    for _ in range(3): await turn(h)
    assert_ready(h,'needs_user')
    assert h.call._toolbox.phase=='dialing'
    assert speaker_decisions(h)[-1].policy_decision['reason_code']==reason
    assert not fallback(h)  # capped speaker decision owns the notifications


async def test_empty_records_no_jev_closes_and_quiet_handoffs(live528):
    h=live528
    await turn(h,'  ')
    d=speaker_decisions(h)[-1]
    assert d.policy_decision['speaker_class']=='empty'
    assert d.policy_decision['no_jev'] is True
    assert d.policy_decision['reason_code']=='speaker_empty_transcript'
    assert d.jev_response=={'answers':{}}
    assert d.jev_request['state']['transcript']['lines']==[]
    assert not h.deps.jev.calls
    assert h.call._media_gate.output_state=='closed'
    await h.silence(4980); assert not h.events('handoff_ready')
    await h.silence(20); await settle(300)
    assert_ready(h,'needs_user','speaker_quiet_after_empty')
    assert fallback(h)[-1].policy_decision['source_decision_id']==d.decision_id


async def test_empty_revokes_allowed_response_and_does_not_reask(live528):
    h=live528; h.call._keypad.deps.jev=FakeJev(default=script(key='none'))
    await turn(h); g=h.call._media_gate; g.bind_response((1,1)); epoch=g.output_epoch
    await turn(h,'')
    assert g.output_state=='closed' and g.output_epoch>epoch
    assert not g.response_audible((1,1))
    assert g.bind_response((1,2))==(2,g.output_epoch)
    assert len(h.call._keypad.deps.jev.calls)==2
    assert speaker_decisions(h)[-1].policy_decision['speaker_class']=='empty'


@pytest.mark.parametrize('transfer',['none','pending'])
async def test_empty_makes_inflight_r2_stale(live528,transfer):
    h=live528; release=asyncio.Event(); entered=asyncio.Event()
    async def ask(state, questions):
        entered.set(); await release.wait()
        return m.JevResponse(answers={k: v for k, v in (answers(transfer=transfer)).items() if k in questions})
    h.call._keypad.deps.jev=SimpleNamespace(ask=ask)
    await turn(h); assert entered.is_set()
    await turn(h,' '); release.set(); await settle(500)
    assert h.call._media_gate.output_state=='closed'
    assert not h.events('dtmf_sent') and not h.events('handoff_ready')
    assert [d.policy_decision['speaker_class'] for d in speaker_decisions(h)]==['empty']
    assert h.call._keypad.policy_state.guard==(transfer=='pending')


@pytest.mark.parametrize('guard',[False,True])
async def test_empty_rebinds_candidate_preserving_token_mark(live528,guard):
    h=live528; reply(h,ph=.2,transfer='pending' if guard else 'none')
    await turn(h); await h.silence(2400)
    c=h.call._quiet_candidate; token=c.token; mark=c.mark
    for _ in range(3): await turn(h,' ')
    c=h.call._quiet_candidate
    assert c.seq==4 and c.token==token and c.mark is mark
    assert h.call._keypad.policy_state.uncertain_run==(0 if guard else 1)
    assert h.call._stuck is None if guard else h.call._stuck is not None
    await h.silence(2600); await settle(300)
    assert_ready(h,'needs_user','speaker_quiet_in_guard' if guard else 'speaker_quiet_after_uncertain')
    assert fallback(h)[-1].action['turn_seq']==4


async def test_new_nonempty_final_invalidates_before_clear_await(live528):
    h=live528; reply(h,ph=.2); await turn(h)
    g=h.call._media_gate; before=h.call._quiet_candidate.token
    entered=asyncio.Event(); release=asyncio.Event(); cleaner=g._cleaner
    async def blocked():
        entered.set(); await release.wait()
        if cleaner is not None: await cleaner()
    g.set_output_cleaner(blocked)
    task=asyncio.create_task(h.sonic.emit_user_text('Next company line'))
    try:
        await entered.wait()
        assert h.call._quiet_candidate is None
        assert h.call._quiet_token!=before
    finally:
        release.set(); await task


@pytest.mark.parametrize('disturbance',['voiced','residue','gap','format','disconnect'])
async def test_locked_quiet_recheck_rejects_then_can_trigger_again(live528,disturbance):
    h=live528; reply(h,ph=.2); await turn(h)
    lock=h.call._handoff_lock; await lock.acquire()
    try:
        await h.silence(5000)
        assert h.call._quiet_candidate.inflight is not None
        if disturbance=='voiced': await h.speech(20)
        elif disturbance=='residue':
            await h.transport.input().feed(in_frame(b'\xff\x7f',h.rate)); await settle()
        elif disturbance=='gap':
            await h.clock.advance(300); await h.frame(zero_pcm16(20,h.rate))
        elif disturbance=='format':
            await h.transport.input().feed(in_frame(zero_pcm16(20,8000),8000)); await settle()
        else: await h.clock.advance(201)
    finally: lock.release()
    await settle(300)
    assert not h.events('handoff_announced') and not fallback(h)
    assert h.call._quiet_candidate.inflight is None
    await h.silence(5040); await settle(300)
    assert_ready(h,'needs_user','speaker_quiet_after_uncertain')


async def test_verdict_delay_counts_from_final(live528):
    h=live528; release=asyncio.Event()
    async def ask(state, questions): await release.wait(); return m.JevResponse(answers={k: v for k, v in (answers(ph=.2)).items() if k in questions})
    h.call._keypad.deps.jev=SimpleNamespace(ask=ask)
    # This fixture intentionally permits a slow offline verdict.
    h.settings.engine.speaker.verdict_timeout_ms=6000
    await turn(h); await h.silence(5000); assert not h.events('handoff_ready')
    release.set(); await settle(500)
    assert_ready(h,'needs_user','speaker_quiet_after_uncertain')
    p=fallback(h)[-1].policy_decision
    assert p['quiet_audio_ms']==p['quiet_wall_ms']==5000


@pytest.mark.parametrize('mode',['uncertain','unavailable','empty'])
async def test_stuck_without_audio_triggers_at_configured_deadline(live528,mode):
    h=live528
    if mode=='uncertain': reply(h,ph=.2)
    elif mode=='unavailable': h.call._keypad.deps.jev=None
    await turn(h,' ' if mode=='empty' else 'Company question')
    source=speaker_decisions(h)[-1]
    await h.clock.advance(19999); await settle(); assert not h.events('handoff_ready')
    await h.clock.advance(1); await settle(500)
    assert_ready(h,'needs_user','speaker_stuck_timeout')
    d=fallback(h)[-1]; assert d.policy_decision['source_decision_id']==source.decision_id
    assert 'quiet_audio_ms' not in d.policy_decision


async def test_stuck_new_finals_do_not_extend_original_deadline(live528):
    h=live528; reply(h,ph=.2); await turn(h)
    source=speaker_decisions(h)[-1].decision_id
    await h.clock.advance(7000)
    h.call._keypad.deps.jev=None; await turn(h)
    await h.clock.advance(7000); await turn(h,' ')
    await h.clock.advance(6000); await settle(400)
    assert_ready(h,'needs_user','speaker_stuck_timeout')
    assert fallback(h)[-1].policy_decision['source_decision_id']==source


@pytest.mark.parametrize('winner',['r2','guard'])
async def test_stuck_lock_recheck_loses_to_r2_or_guard(live528,winner):
    h=live528; reply(h,ph=.2); await turn(h)
    await h.call._handoff_lock.acquire()
    try:
        await h.clock.advance(20000); await settle()
        reply(h,transfer='pending' if winner=='guard' else 'none')
        await turn(h)
    finally: h.call._handoff_lock.release()
    await settle(300)
    assert not h.events('handoff_ready') and not fallback(h)
    assert h.call._stuck is None


async def test_stale_guard_tightens_synchronously_and_preserves_queued_epoch(live528):
    h=live528; g=h.call._media_gate; k=h.call._keypad
    reply(h); await turn(h)
    release=asyncio.Event(); entered=asyncio.Event(); requests=[]
    async def ask(state,questions):

        if 'speaker' in questions: requests.append(state)
        if state.transcript.lines[-1].text in ('Company question', 'Company line'):
            entered.set(); await release.wait()
            return m.JevResponse(answers={k: v for k, v in (answers(transfer='pending')).items() if k in questions})
        return m.JevResponse(answers={k: v for k, v in (answers()).items() if k in questions})
    k.deps.jev=SimpleNamespace(ask=ask)
    await turn(h); assert entered.is_set()
    await turn(h,'Queued latest line')
    # Model a still-allowed R2 permission when the late guard arrives.
    await g.resolve_speaker(3,'virtual_agent')
    blocked=asyncio.Event(); cleaned=asyncio.Event(); cleaner=g._cleaner
    async def clear():
        blocked.set(); await cleaned.wait()
        if cleaner is not None: await cleaner()
    g.set_output_cleaner(clear)
    observations=[]; original=k.on_guard
    def observer():
        original()
        observations.append((g.output_state,k._latest_epoch==g.output_epoch,
            h.call._toolbox.phase,h.call._stuck,h.call._quiet_candidate is not None))
    k.on_guard=observer
    try:
        release.set(); await settle(500)
        assert blocked.is_set(), 'late guard must start output cleanup'
        assert observations==[('closed',True,'hold',None,True)]
        assert k.policy_state.guard and not h.events('dtmf_sent')
    finally: cleaned.set()
    await settle(700)
    assert len(requests)==2 and requests[-1].transcript.lines[-1].text=='Queued latest line'
    assert g.output_state=='closed' and not h.events('dtmf_sent')


async def test_guard_music_never_starts_stuck_or_quiet_handoff(live528):
    h=live528; reply(h,transfer='pending'); await turn(h)
    for _ in range(4):
        await turn(h,' '); await h.speech(5000)
    assert h.call._stuck is None and not h.events('handoff_ready')
    assert h.call._toolbox.phase=='hold'


async def test_unavailable_has_no_quiet_candidate_and_next_final_recovers(live528):
    h=live528; h.call._keypad.deps.jev=None; await turn(h)
    assert h.call._quiet_candidate is None
    await h.silence(5000); assert not h.events('handoff_ready')
    reply(h); await turn(h)
    assert h.call._media_gate.output_state=='allowed' and h.call._stuck is None
    assert not h.events('injection_sent')


async def test_finish_cancels_candidates_timers_and_observers(live528):
    h=live528; reply(h,ph=.2); await turn(h)
    await h.call.hangup('test')
    assert h.call._quiet_candidate is None and h.call._stuck is None
    assert not h.call._media_gate.inbound_observers
    assert h.call._finish_task.done()

async def test_guard_hold_keeps_hold_gate_and_phase(live528):
    h=live528; reply(h,who='hold',transfer='pending'); await turn(h)
    assert h.call._media_gate.output_state=='hold'
    assert h.call._toolbox.phase=='hold'
    assert h.call._quiet_candidate is not None and h.call._stuck is None

async def test_configured_quiet_and_stuck_thresholds_are_used(live528):
    h=live528
    h.settings.engine.speaker.uncertain_max_wait_ms=600
    reply(h,ph=.2); await turn(h)
    await h.clock.advance(599); await settle(); assert not h.events('handoff_ready')
    await h.clock.advance(1); await settle(300)
    assert_ready(h,'needs_user','speaker_stuck_timeout')

async def test_overlapping_empty_final_cannot_close_later_nonempty_turn(live528):
    h=live528; reply(h); g=h.call._media_gate
    entered=asyncio.Event(); release=asyncio.Event()
    async def blocked(): entered.set(); await release.wait()
    g.set_output_cleaner(blocked)
    first=asyncio.create_task(h.sonic.emit_user_text(' '))
    await entered.wait()
    second=asyncio.create_task(h.sonic.emit_user_text('Latest nonempty company line'))
    await settle()
    release.set(); await asyncio.gather(first,second); await settle(500)
    assert g.company_seq==2 and g.output_state=='allowed'
    assert speaker_decisions(h)[-1].action['turn_seq']==2
    assert h.call._stuck is None
