"""Real Carrier JSONL, real Trace/Store and E183c boundary fixtures."""
import asyncio
import copy
import json
import itertools
from types import SimpleNamespace

import pytest
from waitless import cli, models as m
from waitless.company.carrier import CarrierCall
from waitless.company.config import load_company, load_scenario
from waitless.company.engine import ScenarioEngine
from waitless.store import Store
from waitless.trace import Trace
from tests.lane_B.test_sc2_scripted_turns import VOICE, SILENCE
from tests.lane_B.test_i19b_facts import local_call, Line


_trace_serial = itertools.count()

def gate_events(old=True, zero=False):
    def e(event_type, t, **action):
        extra = {}
        if event_type == 'company_turn':
            extra = dict(speaker='company', transcript='Local fixture.')
        if event_type == 'decision':
            extra = dict(decision_id='speaker-fixture', decision_point='agent_tool',
                jev_request={}, jev_response={}, policy_decision={'intent': 'speaker', 'action': 'handoff'})
        return m.TraceEvent(task_id='task-facts', call_id='call-facts', event_type=event_type, t_ms=t, action=action, **extra)
    out = [e('call_started', 5000), e('company_turn', 9000, seq=1, kind='speech'),
        e('output_revoked', 9000, trigger_seq=1, revoked_seq=0, revoked_epoch=0, new_epoch=1, state='pending', clear_id=1),
        e('output_cleared', 9010, clear_id=1, covers=[], ok=True)]
    if old:
        out.append(e('response_bound', 9200, identity='1:1', seq=1, epoch=1))
    out.append(e('output_allowed', 9300, seq=1, epoch=1, speaker='virtual_agent'))
    out += [e('company_turn', 10600, seq=2, kind='speech'),
        e('output_revoked', 10600, trigger_seq=2, revoked_seq=1, revoked_epoch=1, new_epoch=2, state='pending', clear_id=2)]
    if old:
        out.append(e('response_audio', 10600, identity='1:1', first_ms=None if zero else 9400,
                     last_ms=None if zero else 10500, frames=0 if zero else 2))
    out += [e('output_cleared', 10620, clear_id=2, covers=[], ok=True),
        e('decision', 10621, turn_seq=2),
        e('output_revoked', 10622, trigger_seq=None, revoked_seq=2, revoked_epoch=2, new_epoch=3, state='handoff', clear_id=3),
        e('output_cleared', 10630, clear_id=3, covers=[], ok=True),
        e('handoff_announced', 10640), e('handoff_ready', 10641)]
    return out


@pytest.fixture
async def record(tmp_path, company_dir, engine_config, mode_env):
    company = load_company(company_dir, 'lakeshore-energy')
    scenario = load_scenario(company_dir, company.company_id, 'baseline')
    call = CarrierCall(dial=m.CarrierDialIn(call_id='call-facts', task_id='task-facts',
        to_number=company.line_number, stream_url=mode_env('live-sim')['WAITLESS_LINE_WS_URL'],
        stream_token='local-fixture'), call_sid='CA'+'a'*32, scenario_id='baseline', company=company)
    call._stats_path = tmp_path / 'facts.jsonl'
    call._scenario = scenario
    call._engine = ScenarioEngine(scenario, company)
    call._engine_config = engine_config
    call._agent_mode = 'scripted'
    for role, start, end, outcome in [('ivr',5.,6.,None), ('rep',10.,11.,None), ('rep',12.,12.1,'confirmed')]:
        line = m.CompanyLine(step_id='rep' if role == 'rep' else 'greeting', role=role,
            kind='speech' if role == 'rep' else 'prompt', text='Local fixture.',
            expect=m.EvalExpect(intent='confirm_and_end', outcome=outcome) if outcome else None)
        call._record_line_sent(line, start, end)
    call._rep_first_audio_at, call._rep_last_audio_at, call._rep_first_line_end_at = 10., 12., 11.
    async def finish():
        call._freeze_end('company', 'scenario_end')
        await call.hangup()
        return json.loads(call._stats_path.read_text())
    return call, finish


def check(tmp_path, row, events=None, *, ready=12000, windows=(), config=None):
    path = tmp_path / 'selected.jsonl'
    path.write_text(json.dumps(row)+'\n')
    stats = cli._read_company_stats(path, 'call-facts', offset=0)
    store=Store(tmp_path / f'trace-{next(_trace_serial)}.db')
    trace=Trace(store,SimpleNamespace(monotonic_ms=lambda:0))
    try:
        for ev in gate_events() if events is None else events:
            if ev.t_ms is None:
                store.insert_turn(ev)  # Deliberate corrupt row; the normal writer fills missing times.
            else:
                trace.event(ev)
        return cli._identity_check(trace.events(call_id='call-facts'), stats,
            holder_ready_ms=ready, rep_final_ms=None, config=config or SimpleNamespace(
                clear_max_ms=1000, transit_max_ms=100, handoff_latency_max_ms=1500), holder_windows=windows, dtmf_key_ms=500)
    finally:
        store.close()



@pytest.mark.parametrize('old,zero', [(True,False), (False,False), (True,True)])
async def test_missing_first_final_and_zero_frame_positive(record, tmp_path, old, zero):
    _, finish = record
    row = await finish()
    assert check(tmp_path, row, gate_events(old,zero))


async def test_real_pcm_jsonl_span_cannot_move_across_ready(record, tmp_path):
    call, finish = record
    call._observe_inbound(VOICE[:160], 13.99)
    call._observe_inbound(SILENCE[:160], 17.11)
    row = await finish()
    assert row['nonsilent_inbound_spans_during_rep'] == [[13.99,17.11]]
    assert not check(tmp_path,row,ready=14000,windows=[(17100,18100)])
    # Both short gaps and a crossing of the holder window's end must fail.
    row['nonsilent_inbound_spans_during_rep'] = [[13.99,14.01]]
    assert not check(tmp_path,row,ready=14000,windows=[(14000,15000)])
    row['nonsilent_inbound_timestamps_during_rep'] = [17.9]
    row['nonsilent_inbound_spans_during_rep'] = [[17.9,18.2]]
    assert not check(tmp_path,row,ready=14000,windows=[(17100,18100)])


@pytest.mark.parametrize('packet', ['long','split','gap','flush'])
@pytest.mark.parametrize('arrival,expected', [(10.719,True),(10.720,False),(11.999,False),(12.,True)])
async def test_real_window_energy_boundaries(record,tmp_path,packet,arrival,expected):
    call, finish = record
    if packet == 'long':
        call._observe_inbound(VOICE + SILENCE*3, arrival)
    elif packet == 'split':
        for pcm in [VOICE,SILENCE,SILENCE,SILENCE]: call._observe_inbound(pcm,arrival)
    elif packet == 'gap':
        call._observe_inbound(VOICE[:160],arrival)
        call._observe_inbound(SILENCE[:160],arrival+.0001)
    else:
        call._observe_inbound(VOICE[:160],arrival)
    row = await finish()
    assert row['nonsilent_inbound_frames_during_rep'] == 1
    assert check(tmp_path,row,windows=[(12000,14000)]) is expected


@pytest.mark.parametrize('case', ['wait','late_allowed','late_bound','after_g','new_text','slow_clear',
    'wrong_clear','failed_clear','missing_summary','duplicate_summary','duplicate_bound',
    'bad_state','bad_speaker','duplicate_clear','bad_epoch','clear_after_notice','resume','missing_g',
    'missing_time','unidentified_text','legacy_resume_text','resuming_status','handoff_end',
    'new_dtmf','queued_dtmf_tail','missing_key_count','missing_spans','reversed_span_ends','tone_missing','tone_schema',
    'bad_sid_schema','bad_reason_schema','bad_holder_schema','bad_line_schema','tone_before_ready','tone_outside_call','tone_overlap','tone_unsorted','open_tone_not_last'])
async def test_contradictory_facts_fail(record,tmp_path,case):
    _, finish = record
    row = await finish()
    events = gate_events()
    def event(kind): return next(e for e in events if e.event_type==kind)
    if case=='wait': event('decision').policy_decision['action']='wait'
    elif case=='late_allowed': event('output_allowed').t_ms=10001
    elif case=='late_bound':
        event('response_bound').t_ms=10001
        event('response_audio').action.update(first_ms=10002,last_ms=10500)
    elif case=='after_g': event('response_audio').action['last_ms']=10601
    elif case in {'new_text','unidentified_text'}:
        events.append(m.TraceEvent(task_id='task-facts',call_id='call-facts',event_type='waitless_utterance',
            speaker='waitless',transcript='Local fixture.',t_ms=10700,
            action={'response_identity':'new'} if case=='new_text' else {}))
    elif case=='slow_clear':
        for e in events:
            if e.t_ms>=10620:e.t_ms+=1001
    elif case=='wrong_clear':
        events=[e for e in events if not(e.event_type=='output_cleared' and e.action['clear_id']==2)]
    elif case=='failed_clear':
        next(e for e in events if e.event_type=='output_cleared' and e.action['clear_id']==2).action['ok']=False
    elif case=='missing_summary': events=[e for e in events if e.event_type!='response_audio']
    elif case in {'duplicate_summary','duplicate_bound','duplicate_clear'}:
        kind={'duplicate_summary':'response_audio','duplicate_bound':'response_bound','duplicate_clear':'output_revoked'}[case]
        events.insert(events.index(event(kind))+1,copy.deepcopy(event(kind)))
    elif case=='bad_state':event('output_revoked').action['state']='invalid'
    elif case=='bad_speaker':event('output_allowed').action['speaker']='hold'
    elif case=='bad_epoch':event('output_revoked').action['new_epoch']=10
    elif case=='clear_after_notice':
        next(e for e in events if e.event_type=='output_cleared' and e.action['clear_id']==3).t_ms=10700
    elif case=='resume':events.append(m.TraceEvent(task_id='task-facts',call_id='call-facts',event_type='resume_injected',t_ms=12000))
    elif case=='missing_g':events=[e for e in events if not(e.event_type=='company_turn' and e.action['seq']==2)]
    elif case=='missing_time':event('output_allowed').t_ms=None
    elif case in {'legacy_resume_text','resuming_status','handoff_end'}:
        kind={'legacy_resume_text':'injection_sent','resuming_status':'status_updated','handoff_end':'handoff_ended'}[case]
        action={'legacy_resume_text':{'text':'handoff.resume'},'resuming_status':{'phase':'resuming'},'handoff_end':{'dropped_sonic_outputs':0}}[case]
        events.append(m.TraceEvent(task_id='task-facts',call_id='call-facts',event_type=kind,t_ms=12000,
            decision_id='speaker-fixture' if kind=='injection_sent' else None,action=action))
    elif case in {'new_dtmf','queued_dtmf_tail','missing_key_count'}:
        events.append(m.TraceEvent(task_id='task-facts',call_id='call-facts',event_type='dtmf_sent',
            t_ms=12000 if case=='new_dtmf' else 9000,decision_id='speaker-fixture',
            action={} if case=='missing_key_count' else {'keys':4}))
    elif case=='missing_spans':del row['nonsilent_inbound_spans_during_rep']
    elif case=='reversed_span_ends':
        row.update(nonsilent_inbound_frames_during_rep=2,nonsilent_inbound_timestamps_during_rep=[10.61,10.62],
            nonsilent_inbound_spans_during_rep=[[10.61,10.67],[10.62,10.66]])
    elif case=='tone_missing':del row['dtmf_tones']
    elif case=='tone_schema':row['dtmf_tones']=[{'start_at':8.,'digits':0}]
    elif case=='bad_sid_schema':row['call_sid']='invalid'
    elif case=='bad_reason_schema':row['ended']['reason']=False
    elif case=='bad_holder_schema':row['holder']['keypad_entries_complete']=True
    elif case=='bad_line_schema':row['lines'][0]['outcome']={}
    elif case=='tone_before_ready':row['dtmf_tones']=[{'start_at':10.7,'end_at':10.71,'digits':0}]
    elif case=='tone_outside_call':row['dtmf_tones']=[{'start_at':1.,'end_at':2.,'digits':0}]
    else:
        row['dtmf_tones']=[{'start_at':7.,'end_at':8.,'digits':0},{'start_at':8.1,'end_at':8.2,'digits':0}]
        if case=='tone_overlap':row['dtmf_tones'][1]['start_at']=7.9
        elif case=='tone_unsorted':row['dtmf_tones'].reverse()
        else:row['dtmf_tones'][0]['end_at']=None
    assert not check(tmp_path,row,events)


@pytest.mark.parametrize('case',['covers','old_tail','late_text','slow_join','own_keys','content_tail'])
async def test_positive_controls(record,tmp_path,case):
    call,finish=record
    events=gate_events()
    if case=='old_tail':call._observe_inbound(VOICE,10.719)
    row=await finish()
    if case=='covers':
        events=[e for e in events if not(e.event_type=='output_cleared' and e.action['clear_id']==2)]
        next(e for e in events if e.event_type=='output_cleared' and e.action['clear_id']==3).action['covers']=[2]
    elif case=='late_text':events.append(m.TraceEvent(task_id='task-facts',call_id='call-facts',event_type='waitless_utterance',
        speaker='waitless',transcript='Local fixture.',t_ms=10700,action={'response_identity':'1:1'}))
    elif case=='own_keys':row['dtmf_tones']=[{'start_at':12.1,'end_at':12.2,'digits':1}]
    elif case=='content_tail':
        row['rep_last_audio_at']=10.99
        row['lines']=row['lines'][:2]
        row['ended'].update(closing_index=None,closing_outcome=None,closing_line_completed=False)
    assert check(tmp_path,row,events,ready=20000 if case=='slow_join' else 12000)


async def test_bank_tone_even_after_ready_fails(record,tmp_path):
    _,finish=record
    row=await finish()
    row['company_id']='bluefin-harbor-bank'
    row['dtmf_tones']=[{'start_at':12.1,'end_at':12.1,'digits':0}]
    assert not check(tmp_path,row)


async def test_notice_compares_unrounded_and_can_be_early(record,tmp_path):
    _,finish=record
    row=await finish()
    events=gate_events()
    next(e for e in events if e.event_type=='handoff_ready').t_ms=12501
    row['rep_first_line_end_at']=11.0006  # 12501 - 11000.6 = 1500.4
    row['lines'][1]['ended_at']=11.0006
    assert not check(tmp_path,row,events,ready=13000)
    row['rep_first_line_end_at']=11.001
    row['lines'][1]['ended_at']=11.001
    assert check(tmp_path,row,events,ready=13000)



async def test_stats_offset_unique_and_bad_rows(record,tmp_path):
    call,finish=record
    row=await finish()
    path=tmp_path/'offset.jsonl'
    path.write_text('bad old row\n')
    offset=path.stat().st_size
    with path.open('a') as out:
        out.write(json.dumps({**row,'call_id':'other'})+'\n'+json.dumps(row)+'\n')
    assert cli._read_company_stats(path,'call-facts',offset=offset)['call_id']=='call-facts'
    assert cli._read_company_stats(path,'call-facts',offset=0)=={}
    with path.open('a') as out:out.write(json.dumps(row)+'\n')
    assert cli._read_company_stats(path,'call-facts',offset=offset)=={}


@pytest.mark.parametrize('content_bytes',[241,320])
async def test_real_carrier_run_jsonl_to_full_consumer(local_call,tmp_path,monkeypatch,content_bytes):
    call,_=local_call
    monkeypatch.setattr(call,'_line_audio',lambda line:b'\xff'*(160 if line.role=='ivr' else content_bytes))
    await call.run()
    row=json.loads(call._stats_path.read_text())
    r=int(row['rep_first_audio_at']*1000)
    events=gate_events(old=False)
    # Move the complete valid permission history into this call's real clock.
    delta=r-10600
    for e in events:e.t_ms+=delta
    for e in events:
        if e.event_type=='company_turn' and e.action['seq']==2:e.t_ms=r
    # Notify in the first content frame, before the closing tail drains.
    for kind,offset in [('output_cleared',0),('decision',0),('output_revoked',0),
                        ('handoff_announced',1),('handoff_ready',1)]:
        for e in events:
            if e.event_type==kind and e.t_ms>=r:e.t_ms=r+offset
    assert check(tmp_path,row,events,ready=r+2)


def test_first_answer_gate_uses_voice_then_quiet_or_no_voice_timeout():
    from waitless.live_check import AnswerGate
    gate=AnswerGate(ready_ms=0,silence_ms=1000,no_voice_ms=1500)
    assert not gate.due(1499) and gate.due(1500)
    gate.observe(1400,voiced=True)
    assert not gate.due(1500) and not gate.due(2399) and gate.due(2400)
    gate.observe(2300,voiced=True)
    assert not gate.due(3299) and gate.due(3300)
    with pytest.raises(ValueError):AnswerGate(ready_ms=0,silence_ms=None,no_voice_ms=1500)


@pytest.mark.parametrize('field',['clear_max_ms','transit_max_ms','handoff_latency_max_ms'])
async def test_required_threshold_missing_is_configuration_error(record,tmp_path,field):
    _,finish=record
    row=await finish()
    config=SimpleNamespace(clear_max_ms=1000,transit_max_ms=100,handoff_latency_max_ms=1500)
    delattr(config,field)
    with pytest.raises(ValueError):check(tmp_path,row,config=config)


@pytest.mark.parametrize('change',[None,'before_outcome','before_awaiting','response_call','response_source','after_source','after_awaiting'])
def test_user_result_is_separate_and_bound_to_call(change):
    from waitless.live_check import result_check
    before={'outcome':'needs_follow_up','awaiting_user_result':True}
    response={'call_id':'call-facts','outcome':'confirmed','result_source':'user','awaiting_user_result':False}
    after=dict(response)
    if change=='before_outcome':before['outcome']='confirmed'
    elif change=='before_awaiting':before['awaiting_user_result']=False
    elif change=='response_call':response['call_id']='other'
    elif change=='response_source':response['result_source']='transcript'
    elif change=='after_source':after['result_source']='transcript'
    elif change=='after_awaiting':after['awaiting_user_result']=True
    assert result_check(before,response,after,call_id='call-facts',expected_outcome='confirmed') is (change is None)


async def test_first_reply_waits_until_current_phrase_finishes():
    from tests.lane_A.test_u119_holder import FlowSocket, live_plan, TONE
    ws=FlowSocket()
    sent_times=[]
    original=ws.send
    async def send(value):
        sent_times.append(asyncio.get_running_loop().time())
        await original(value)
    ws.send=send
    last=[None]
    async def current_phrase():
        for _ in range(6):
            last[0]=asyncio.get_running_loop().time()
            await ws.incoming.put(TONE)
            await asyncio.sleep(.02)
    producer=asyncio.create_task(current_phrase())
    try:
        assert await cli._simulate_account_holder(ws,live_plan(),threshold_dbfs=-40,
            question_end_silence_ms=40,frame_ms=20)=='ended'
        await producer
        assert sent_times[0] >= last[0]+.039
    finally:
        producer.cancel()
        await asyncio.gather(producer,return_exceptions=True)
        await ws.cleanup()


@pytest.mark.parametrize('zero',[False,True])
async def test_no_old_audio_never_exempts_wire_voice(record,tmp_path,zero):
    call,finish=record
    call._observe_inbound(VOICE,10.61)
    row=await finish()
    assert not check(tmp_path,row,gate_events(old=zero,zero=zero))


async def test_real_candidate_with_zero_digits_fails_before_ready(record,tmp_path):
    from tests.company._signals import pipecat_dtmf_pcm,ulaw_roundtrip
    call,finish=record
    tone=ulaw_roundtrip(await pipecat_dtmf_pcm('0'))[:320]
    call._observe_inbound(tone+SILENCE*2,10.7)
    row=await finish()
    assert row['dtmf_tones']==[{'start_at':10.7,'end_at':10.7,'digits':0}]
    assert not check(tmp_path,row)


async def test_gate_cannot_return_to_pending_after_handoff(record,tmp_path):
    _,finish=record
    row=await finish()
    events=gate_events()
    g=next(e for e in events if e.event_type=='output_revoked' and e.action['trigger_seq']==2)
    h=next(e for e in events if e.event_type=='output_revoked' and e.action['state']=='handoff')
    gc=next(e for e in events if e.event_type=='output_cleared' and e.action['clear_id']==2)
    hc=next(e for e in events if e.event_type=='output_cleared' and e.action['clear_id']==3)
    h.t_ms=10550
    h.action.update(clear_id=2,revoked_seq=1,revoked_epoch=1,new_epoch=2)
    hc.t_ms=10560
    hc.action['clear_id']=2
    g.action.update(clear_id=3,revoked_seq=1,revoked_epoch=2,new_epoch=3)
    gc.action['clear_id']=3
    next(e for e in events if e.event_type=='response_audio').t_ms=10551
    events.sort(key=lambda e:e.t_ms)
    assert not check(tmp_path,row,events)
