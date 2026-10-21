"""T8 checklist completion using scenario text and real media components."""
import asyncio
from pathlib import Path
from types import SimpleNamespace
import pytest
import yaml
from fakes import FakeJev
from waitless import models as m
from pipecat.processors.frame_processor import FrameDirection
from lane_D.test_u119_gate import audio, MemoryClient, output_for, pending
from call_engine._support import Chain, settle, in_frame, tone_pcm16, zero_pcm16
from lane_v528c2.test_wiring import (live528, agent_call, settings, spec, engine,
    compiled, line_number, store, trace, turn, reply, fallback, assert_ready, speaker_decisions)
from lane_v528a.test_keypad_policy import script
from lane_v528a.test_policy import answers

ROOT=Path(__file__).resolve().parents[3]


def scenario(company, name='baseline'):
    return yaml.safe_load((ROOT/'company'/company/'scenarios'/(name+'.yaml')).read_text())['steps']


@pytest.mark.parametrize('star',[False,True])
async def test_power_original_paths(live528,star):
    h=live528; steps=scenario('lakeshore-energy')
    reply(h,who='menu_or_recording'); await turn(h,steps['service_menu']['text'])
    assert not h.call._keypad.policy_state.guard
    if star:
        h.call._keypad.deps.jev=FakeJev(default=script(key='*'))
        await turn(h,steps['service_menu']['text'])
        assert h.transport.output().dtmf()
        assert not h.call._keypad.policy_state.guard
    else:
        reply(h,transfer='pending'); await turn(h,steps['agent']['text_script'][-1]['say'])
        assert h.call._toolbox.phase=='hold' and not h.events('handoff_ready')
    reply(h,transfer='connected')
    await turn(h,steps['rep']['live_view']['text_script'][0]['say'])
    assert len(h.events('handoff_ready'))==1
    assert h.call.status().handoff.reason_kind=='transfer_connected'
    assert speaker_decisions(h)[-1].policy_decision['guard'] is True
    if star:
        assert any('[keys pressed: *]'==line.text for line in h.call._keypad._state().transcript.lines)


@pytest.mark.parametrize('hold_s',[180,1200])
async def test_bank_original_split_announcement_and_long_voiced_hold(live528,hold_s):
    h=live528; steps=scenario('bluefin-harbor-bank','long-hold' if hold_s==1200 else 'baseline')
    text=steps['agent']['text_script'][-1]['say']
    pieces=text.split('. ')
    reply(h,transfer='pending')
    for piece in pieces: await turn(h,piece)
    await turn(h,steps['queue_notice']['text'])
    # 200 ms frames delivered every 200 ms: real continuous inbound audio,
    # never a single backlog frame masquerading as 1200 seconds of wall time.
    pcm=tone_pcm16(200,h.rate)
    for _ in range(hold_s*5):
        await h.clock.advance(200)
        await h.transport.input().feed(in_frame(pcm,h.rate))
        await settle(5)
    assert not h.events('handoff_ready') and h.call._stuck is None
    assert h.call._toolbox.phase=='hold'
    reply(h,transfer='connected'); await turn(h,steps['rep']['text_script'][0]['say'])
    assert_ready(h,'transfer_connected')


@pytest.mark.parametrize('evidence',['queue_notice','same_package'])
async def test_bank_missing_announcement(live528,evidence):
    h=live528; steps=scenario('bluefin-harbor-bank')
    if evidence=='queue_notice':
        reply(h,transfer='pending'); await turn(h,steps['queue_notice']['text'])
    reply(h,transfer='connected'); await turn(h,steps['rep']['text_script'][0]['say'])
    assert_ready(h,'transfer_connected')


@pytest.mark.parametrize('text',[
    'Before I connect you, what is your account number?',
    'To keep your place in line, press 1. For a callback, press 2.',
    'Would you like me to connect you to a person?'])
async def test_connected_negative_examples_use_progress_notification(live528,text):
    h=live528; reply(h,transfer='pending'); await turn(h)
    reply(h,transfer='connected'); await turn(h,text)
    assert_ready(h,'transfer_connected')
    assert h.call.status().handoff.reason==h.settings.engine.handoff.connected_text


@pytest.mark.parametrize('confidence,guard,state',[(.7,True,'closed'),(.69,False,'allowed')])
async def test_first_offer_boundary(live528,confidence,guard,state):
    h=live528; reply(h,transfer='pending',tc=confidence)
    await turn(h,'Would you like me to connect you to a person?')
    assert h.call._keypad.policy_state.guard is guard
    assert h.call._media_gate.output_state==state and not h.events('handoff_ready')


async def test_coalesced_announcement_is_in_representative_request(live528):
    h=live528; entered=asyncio.Event(); release=asyncio.Event(); requests=[]
    async def ask(state,questions):

        if 'speaker' in questions: requests.append(state)
        if state.transcript.lines[-1].text in ('Company question', 'Company line'):
            entered.set(); await release.wait()
            return m.JevResponse(answers={k: v for k, v in (answers()).items() if k in questions})
        return m.JevResponse(answers={k: v for k, v in (answers(transfer='connected')).items() if k in questions})
    h.call._keypad.deps.jev=SimpleNamespace(ask=ask)
    await turn(h,'Company question'); assert entered.is_set()
    steps=scenario('lakeshore-energy')
    await turn(h,steps['agent']['text_script'][-1]['say'])
    await turn(h,steps['rep']['live_view']['text_script'][0]['say'])
    release.set(); await settle(600)
    assert len(requests)==2 and len(requests[-1].transcript.lines)==3
    assert_ready(h,'transfer_connected')


async def test_stuck_despite_continuous_background_audio(live528):
    h=live528; reply(h,ph=.2); await turn(h)
    await h.speech(20000); await settle(300)
    assert_ready(h,'needs_user','speaker_stuck_timeout')


async def test_final_mark_conservatively_excludes_19ms_residue(live528):
    h=live528
    await h.transport.input().feed(in_frame(zero_pcm16(19,h.rate),h.rate)); await settle()
    reply(h,ph=.2); await turn(h)
    await h.clock.advance(20)
    await h.transport.input().feed(in_frame(zero_pcm16(1,h.rate),h.rate)); await settle()
    await h.silence(4980)
    assert not h.events('handoff_ready')
    await h.silence(20); await settle(300)
    assert_ready(h,'needs_user','speaker_quiet_after_uncertain')


async def test_pre_final_quiet_and_sparse_arrivals_do_not_qualify(live528):
    h=live528; await h.silence(5000); reply(h,ph=.2); await turn(h)
    for _ in range(26):
        await h.clock.advance(180); await h.frame(zero_pcm16(20,h.rate))
    assert not h.events('handoff_ready') and not fallback(h)


async def test_empty_rebind_while_fallback_waits_for_lock(live528):
    h=live528; reply(h,ph=.2); await turn(h)
    await h.call._handoff_lock.acquire()
    try:
        await h.silence(5000); await turn(h,' ')
    finally: h.call._handoff_lock.release()
    await settle(300)
    assert_ready(h,'needs_user','speaker_quiet_after_uncertain')
    assert fallback(h)[-1].action['turn_seq']==2


@pytest.mark.parametrize('winner',['live_human','transfer_connected','stuck'])
async def test_quiet_competes_with_other_handoff_once(live528,winner):
    h=live528; reply(h,ph=.2); await turn(h)
    await h.call._handoff_lock.acquire()
    try:
        await h.silence(5000)
        if winner=='stuck': await h.clock.advance(15000)
        else:
            reply(h,who='live_human' if winner=='live_human' else 'virtual_agent',
                confidence=.9,transfer='connected' if winner=='transfer_connected' else 'none')
            await turn(h)
    finally: h.call._handoff_lock.release()
    await settle(500)
    assert len(h.events('handoff_announced'))==len(h.events('handoff_ready'))==1


@pytest.mark.parametrize('mode',['uncertain','guard','unavailable','empty'])
async def test_closed_tiers_drop_old_audio_text_keys_and_next_r2_recovers(live528,mode):
    h=live528; reply(h); await turn(h)
    g=h.call._media_gate; g.bind_response((1,1)); old_epoch=g.output_epoch
    if mode=='unavailable': h.call._keypad.deps.jev=None
    else: reply(h,ph=.2 if mode=='uncertain' else .01,transfer='pending' if mode=='guard' else 'none')
    if mode == 'empty':
        await h.clock.advance(h.settings.engine.speaker.empty_continuation_ms + 1)
    await turn(h,' ' if mode=='empty' else 'Closed company turn')
    before=len(h.transport.output().tts_audio())
    frame=audio(); frame.response_identity=(1,1); frame._waitless_output_epoch=old_epoch
    await g.outbound.process_frame(frame,FrameDirection.DOWNSTREAM)
    ev=m.ProviderTextEvent(role='ASSISTANT',stage='FINAL',text='Discarded response',response=1,
        stop_reason='END_TURN',t_ms=h.clock.monotonic_ms())
    object.__setattr__(ev,'response_identity',(1,1)); await h.call._tap.on_provider_text(ev)
    assert len(h.transport.output().tts_audio())==before
    assert not h.events('waitless_utterance') and not h.events('dtmf_sent')
    assert h.call._toolbox.phase!='live_rep'
    if mode!='guard':
        reply(h); await turn(h)
        assert g.output_state=='allowed'
        await g.outbound.process_frame(frame,FrameDirection.DOWNSTREAM)
        assert len(h.transport.output().tts_audio())==before


async def test_real_fixed_transport_cannot_send_after_guard_revocation(live528):
    h=live528; g=h.call._media_gate; k=h.call._keypad
    client=MemoryClient(); out=output_for(client); out.bind_gate(g)
    async with Chain(g.outbound,out) as chain:
        await pending(g,1); k.begin_turn(1); g.bind_response((1,1))
        await g.resolve_speaker(1,'virtual_agent')
        await chain.push(audio(ms=400)); await asyncio.wait_for(client.media_ready.wait(),3)
        k.policy_state=k.policy_state.__class__(guard=True,guard_since_seq=1)
        h.call._on_guard()
        before=len(client.packets)
        await asyncio.sleep(.09)
        assert len(client.packets)==before or client.packets[-1]['event']=='clear'
        assert g.output_state=='closed' and k._latest_epoch==g.output_epoch
        assert not any(p.get('event')=='media' for p in client.packets[before:])

async def test_low_confidence_single_human_without_next_final_uses_quiet(live528):
    h=live528; reply(h,who='live_human',confidence=.69,ph=.2); await turn(h)
    assert speaker_decisions(h)[-1].policy_decision['speaker_class']=='uncertain'
    await h.silence(5000); await settle(300)
    assert_ready(h,'needs_user','speaker_quiet_after_uncertain')


async def test_quiet_threshold_is_loaded_before_attach(agent_call):
    h=agent_call; h.component_tools=False
    h.settings.engine.speaker.uncertain_quiet_ms=60
    h.settings.engine.speaker.quiet_max_gap_ms=30
    async def ask(state, questions): return m.JevResponse(answers={k: v for k, v in (answers(ph=.2)).items() if k in questions})
    h.deps.jev=SimpleNamespace(ask=ask); await h.boot(); await turn(h)
    await h.silence(40); assert not h.events('handoff_ready')
    await h.silence(20); await settle(300)
    assert_ready(h,'needs_user','speaker_quiet_after_uncertain')
    assert fallback(h)[-1].policy_decision['quiet_audio_ms']==60


@pytest.mark.parametrize('outcome',['recover','stuck'])
async def test_actual_jev_timeout_never_reasks_and_uses_next_final_or_stuck(live528,outcome):
    h=live528; count=0
    async def ask(state, questions):
        nonlocal count
        count+=1; await asyncio.Event().wait()
    h.call._keypad.deps.jev=SimpleNamespace(ask=ask)
    await turn(h); await h.clock.advance(1000); await settle(300)
    assert speaker_decisions(h)[-1].policy_decision['speaker_class']=='unavailable'
    assert not h.events('dtmf_sent') and not h.events('injection_sent')
    if outcome=='recover':
        reply(h); await turn(h)
        assert h.call._media_gate.output_state=='allowed' and h.call._stuck is None
    else:
        await h.clock.advance(20000); await settle(300)
        assert_ready(h,'needs_user','speaker_stuck_timeout')
    assert count==2


# §13: guarded semantic FINALs require a quiet segment before a verdict.
_raw_turn = turn
async def turn(h, text='Company line'):
    from waitless.call.keypad import semantic_company_text
    guard = h.call._keypad.policy_state.guard
    before = len(speaker_decisions(h))
    await _raw_turn(h, text)
    if guard and semantic_company_text(text)[0].strip():
        assert len(speaker_decisions(h)) == before
        await h.silence(h.settings.engine.speaker.guard_segment_quiet_ms)
        await settle(300)
