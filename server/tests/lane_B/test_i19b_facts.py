"""Real CarrierCall with local line I/O; no synthesis or external services."""
import asyncio
import json
from types import SimpleNamespace

import pytest
from websockets.exceptions import ConnectionClosedOK
from websockets.frames import Close

from waitless.company.carrier import CarrierCall, CarrierState
from waitless.company.config import load_company, load_scenario
from waitless.models import CarrierDialIn
from tests.lane_B.test_sc2_scripted_turns import VOICE, SILENCE
from tests.lane_B.test_u112_deposit_window import deposit, keypad
from tests.company.test_audio_carrier_line import send_keys
from tests.lane_B.test_sc3_scripted_carrier import rig, connect_rig, reach_keypad, wait_line, speak
from tests.company._live_harness import wait_until
from waitless.audio.codec import ulaw_decode, ulaw_encode


class Line:
    def __init__(self):
        self.incoming = asyncio.Queue()
        self.sent = []
        self.call = None
        self.failure = None
        self.delay = 0
    async def send(self, raw):
        msg = json.loads(raw)
        if self.delay and msg['event'] == 'media':
            await asyncio.sleep(self.delay)
        if self.failure and msg['event'] == 'media':
            raise self.failure
        self.sent.append((asyncio.get_running_loop().time(), msg))
        if msg['event'] == 'media':
            # An eager mark must not short-circuit the actual content tail.
            await self.incoming.put(json.dumps({'event': 'mark', 'streamSid': self.call._stream_sid,
                                               'mark': {'name': 'done'}}))
    async def recv(self):
        value = await self.incoming.get()
        if isinstance(value, BaseException):
            raise value
        return value
    async def close(self):
        pass


@pytest.fixture
def local_call(company_dir, engine_config, tmp_path, monkeypatch, mode_env):
    company = load_company(company_dir, 'lakeshore-energy')
    scenario = load_scenario(company_dir, company.company_id, 'baseline')
    rep = scenario.steps['rep'].model_dump()
    rep.pop('live_view', None)
    rep.pop('text_view', None)
    rep.update(text_script=[{'say': 'Local closing fixture.', 'expect': {
        'intent': 'confirm_and_end', 'outcome': 'confirmed', 'action': 'end'}}], next=None, on_done=None)
    from waitless.models import ScenarioStep
    scenario = scenario.model_copy(update={'steps': {
        'greeting': scenario.steps['greeting'].model_copy(update={'next': 'rep'}),
        'rep': ScenarioStep.model_validate(rep)}})
    env = mode_env('live-sim', company=True)
    call = CarrierCall(dial=CarrierDialIn(to_number=company.line_number,
        stream_url=env['WAITLESS_LINE_WS_URL'], stream_token='local-fixture',
        call_id='call-facts', task_id='task-facts'), call_sid='CA' + 'a' * 32, scenario_id='baseline', company=company)
    call._scenario = scenario
    call._agent_mode = 'scripted'
    call._engine_config = engine_config
    call._prompt_cache = call._voice_cache = object()
    call._stats_path = tmp_path / 'stats.jsonl'
    monkeypatch.setattr(call, '_line_audio', lambda line: b'\xff' * (160 if line.role == 'ivr' else 241))
    line = Line()
    line.call = call
    async def connect(*args, **kwargs):
        return line
    monkeypatch.setattr('websockets.asyncio.client.connect', connect)
    return call, line


def stats(call):
    rows = [json.loads(s) for s in call._stats_path.read_text().splitlines()]
    assert len(rows) == 1
    return rows[0]


@pytest.mark.parametrize('slow', [0, .025])
async def test_natural_close_tail_and_exact_instance_times(local_call, slow):
    call, line = local_call
    line.delay = slow
    await call.run()
    row = stats(call)
    assert row['schema_version'] == 2
    assert row['company_id'] == call.company.company_id
    assert row['scenario_id'] == 'baseline' and row['agent_mode'] == 'scripted'
    ended = row['ended']
    assert ended['by'] == 'company' and ended['reason'] == 'scenario_end'
    assert ended['closing_line_completed'] is True
    closing = row['lines'][ended['closing_index']]
    assert closing['role'] == 'rep' and closing['intent'] == 'confirm_and_end'
    assert ended['closing_outcome'] == closing['outcome'] == 'confirmed'
    assert closing['started_at'] == row['rep_first_audio_at']
    assert closing['ended_at'] == row['rep_first_line_end_at']
    assert closing['ended_at'] > row['rep_last_audio_at']
    assert next(t for t, m in line.sent if m['event'] == 'stop') >= closing['ended_at']
    assert all(l['started_at'] <= ended['at'] and
               (l['ended_at'] is None or l['ended_at'] <= ended['at']) for l in row['lines'])
    assert [l['index'] for l in row['lines']] == list(range(len(row['lines'])))
    assert row['dtmf_tones'] == []
    assert set(row['holder']) == {'keypad_entries_complete', 'keypad_wrong_length', 'choice',
                                  'choices_recorded', 'holder_voice_answers'}


@pytest.mark.parametrize('source', ['peer', 'api', 'bad', 'cancelled'])
async def test_interruption_freezes_before_cleanup(local_call, source):
    call, line = local_call
    task = asyncio.create_task(call.run())
    await wait_until(lambda: call._rep_first_audio_at is not None, 2, 'rep content')
    if source == 'api':
        await call.hangup(source='api')
    elif source == 'peer':
        await line.incoming.put(ConnectionClosedOK(Close(1000, ''), Close(1000, ''), True))
    elif source == 'bad':
        await line.incoming.put('{}')
    else:
        task.cancel()
    await asyncio.gather(task, return_exceptions=True)
    before = stats(call)
    await call.hangup(source='shutdown')
    assert stats(call) == before
    assert before['ended']['by'] == {'peer': 'peer', 'api': 'api_hangup', 'bad': 'error', 'cancelled': 'error'}[source]
    assert before['ended']['closing_line_completed'] is False
    assert before['lines'][-1]['ended_at'] is None


async def test_pretry_failure_has_full_empty_schema(local_call):
    call, _ = local_call
    call._scenario = None
    state = CarrierState([call.company])
    await state._run_call(call.call_sid, call)
    row = stats(call)
    assert row['ended']['by'] == 'error'
    assert row['lines'] == [] and row['dtmf_tones'] == []
    assert row['nonsilent_inbound_spans_during_rep'] == []
    assert row['holder']['choice'] is None


@pytest.mark.parametrize('split', [False, True])
async def test_real_call_observes_windows_and_partial_on_freeze(local_call, split):
    call, _ = local_call
    call._rep_first_audio_at = 10.
    call._observe_inbound(VOICE + SILENCE * 3, 10.01) if not split else [
        call._observe_inbound(p, 10.01) for p in [VOICE, SILENCE, SILENCE, SILENCE]]
    call._observe_inbound(VOICE[:160], 10.02)
    call._observe_inbound(SILENCE[:160], 14.11)
    call._observe_inbound(VOICE[:160], 14.2)  # Flush must pad and retain this 10 ms.
    await call.hangup(source='api')
    row = stats(call)
    assert row['nonsilent_inbound_frames_during_rep'] == 3
    assert row['nonsilent_inbound_spans_during_rep'] == [[10.01, 10.01], [10.02, 14.11], [14.2, 14.2]]
    assert row['nonsilent_inbound_timestamps_during_rep'] == [10.01, 10.02, 14.2]


@pytest.mark.parametrize('layout', ['packet', 'split', 'gap', 'switch', 'repeat', 'partial'])
async def test_independent_tone_observer(local_call, layout):
    call, _ = local_call
    # Generate with the fixed dependency, never replace GoertzelDetector.
    from tests.company._signals import pipecat_dtmf_pcm, ulaw_roundtrip
    tone = ulaw_roundtrip(await pipecat_dtmf_pcm('0'))[:320]
    other = ulaw_roundtrip(await pipecat_dtmf_pcm('2'))[:640]
    if layout == 'packet':
        call._observe_inbound(tone + SILENCE * 2, 10.)
    elif layout == 'split':
        for p in [tone, SILENCE, SILENCE]:
            call._observe_inbound(p, 10.)
    elif layout == 'gap':
        call._observe_inbound(tone[:160], 10.)
        call._observe_inbound(tone[160:], 14.11)
        call._observe_inbound(SILENCE * 2, 14.2)
    elif layout == 'switch':
        call._observe_inbound(tone * 2 + other + SILENCE * 2, 10.)
    elif layout == 'repeat':
        call._observe_inbound(tone * 2 + SILENCE * 2, 10.)
        call._observe_inbound(tone * 2 + SILENCE * 2, 11.)
    else:
        call._observe_inbound(tone[:304], 10.)
    await call.hangup(source='api')
    tones = stats(call)['dtmf_tones']
    assert len(tones) == (2 if layout == 'repeat' else 1)
    assert tones[0]['start_at'] == 10.
    assert tones[0]['end_at'] == (None if layout == 'partial' else 14.11 if layout == 'gap' else 10.)
    assert tones[0]['digits'] == (2 if layout == 'switch' else 1 if layout == 'repeat' else 0)
    assert all(set(t) == {'start_at', 'end_at', 'digits'} for t in tones)


@pytest.mark.parametrize('key,choice', [('1', 'agree'), ('2', 'decline')])
def test_holder_counters_at_real_mutations(company_dir, engine_config, key, choice):
    engine, turns, _, now = deposit(company_dir, engine_config)
    keypad(engine, turns, key)
    assert engine.holder_facts == {'keypad_entries_complete': 1, 'keypad_wrong_length': 0,
        'choice': choice, 'choices_recorded': 1, 'holder_voice_answers': 0}


async def test_wire_media_and_clear_keep_observer_state(local_call, monkeypatch):
    import base64
    from tests.company._signals import pipecat_dtmf_pcm, ulaw_roundtrip
    from waitless.company.dtmf_detect import GoertzelDetector
    call, line = local_call
    monkeypatch.setattr(call, '_line_audio', lambda _: b'\xff' * 16000)
    reset_ids = []
    original = GoertzelDetector.reset
    def reset(self):
        reset_ids.append(id(self))
        original(self)
    monkeypatch.setattr(GoertzelDetector, 'reset', reset)
    task = asyncio.create_task(call.run())
    await wait_until(lambda: call._started, 2, 'start')
    tone = ulaw_roundtrip(await pipecat_dtmf_pcm('0'))[:320]
    async def media(pcm):
        await line.incoming.put(json.dumps({'event': 'media', 'streamSid': call._stream_sid,
            'media': {'payload': base64.b64encode(ulaw_encode(pcm)).decode()}}))
    await media(tone)
    await line.incoming.put(json.dumps({'event': 'clear', 'streamSid': call._stream_sid}))
    await media(tone)
    await media(SILENCE * 2)
    await wait_until(lambda: call._dtmf_tones and call._dtmf_tones[0]['end_at'] is not None, 2, 'tone release')
    await call.hangup(source='api')
    await task
    row = stats(call)
    assert row['dtmf_tones'][0]['digits'] == 1
    assert id(call._tone_observer) not in reset_ids
    assert len(reset_ids) == 2  # Business detector construction and ClearMsg reset.


async def test_first_frame_send_failure_is_empty_and_frozen(local_call):
    call, line = local_call
    line.failure = RuntimeError('controlled')
    with pytest.raises(RuntimeError):
        await call.run()
    row = stats(call)
    assert row['ended']['by'] == 'error' and row['ended']['reason'] == 'RuntimeError'
    assert row['lines'] == []
    assert row['rep_first_audio_at'] is None


async def test_shutdown_source_passes_through_state(local_call):
    call, _ = local_call
    state = CarrierState([call.company])
    state._calls[call.call_sid] = call
    state._tasks[call.call_sid] = asyncio.create_task(asyncio.Event().wait())
    await state._shutdown()
    assert stats(call)['ended']['reason'] == 'shutdown'


@pytest.mark.parametrize('keys,expected', [('#', 1), ('1#', 1)])
def test_wrong_length_is_recorded_where_rejected(company_dir, engine_config, keys, expected):
    from tests.lane_B.test_u112_deposit_window import window, play
    engine, turns, _, lines, now = window(company_dir, engine_config)
    play(turns, lines, now)
    keypad(engine, turns, keys)
    assert engine.holder_facts['keypad_wrong_length'] == expected
    assert engine.holder_facts['keypad_entries_complete'] == 0
    assert engine.holder_facts['choices_recorded'] == 0 and engine.holder_facts['choice'] is None


@pytest.mark.parametrize('key,choice,outcome', [('1','agree','confirmed'),('2','decline','needs_follow_up'),(None,None,'needs_follow_up')])
async def test_electric_business_stats_on_real_local_line(rig,key,choice,outcome):
    import re
    from tests.lane_B.test_u112_deposit_window import DOB, DEPOSIT, AGREE, DECLINE
    settings,_,polly,times,_,company,scenario=rig
    async with connect_rig(rig) as (_,call,conn):
        await reach_keypad(conn,rig)
        await send_keys(conn,re.sub(r'\D','',company.customers[0].phone_number)+'#')
        await wait_line(conn,polly,times,scenario.steps['rep'].text_script[0].say)
        await speak(conn,200)
        await wait_line(conn,polly,times,DOB)
        await send_keys(conn,'12345678#')
        await wait_line(conn,polly,times,DEPOSIT)
        if key:
            await send_keys(conn,key)
            await wait_line(conn,polly,times,AGREE if key=='1' else DECLINE)
        await wait_until(lambda:conn.closed_at is not None,25,'business stop')
    row=json.loads(settings.company_stats_path.read_text())
    assert row['ended']['by']=='company' and row['ended']['reason']=='scenario_end'
    assert row['ended']['closing_line_completed'] is True
    closing=row['lines'][row['ended']['closing_index']]
    assert closing['role']=='rep' and closing['outcome']==outcome
    assert closing['intent']==('confirm_and_end' if outcome=='confirmed' else 'wait')
    assert row['holder']['choice']==choice and row['holder']['choices_recorded']==int(key is not None)
    assert row['holder']['keypad_entries_complete']==1 and row['holder']['keypad_wrong_length']==0
    assert row['holder']['holder_voice_answers']==0
    assert '12345678' not in json.dumps(row)
    assert all('text' not in line and 'keys' not in line for line in row['lines'])


async def test_bot_playback_never_claims_completion(local_call,monkeypatch):
    import base64
    call,line=local_call
    call._agent_mode='sonic'
    call._bot_endpoint=call.dial.stream_url
    class Bot(Line):
        async def send(self,raw):
            self.sent.append((asyncio.get_running_loop().time(),json.loads(raw)))
        def __aiter__(self):return self.stream()
        async def stream(self):
            for _ in range(2):
                yield json.dumps({'event':'media','streamSid':call._stream_sid,
                    'media':{'payload':base64.b64encode(b'\xff'*81).decode()}})
            call._engine._finish()
    bot=Bot();bot.call=call
    count=0
    async def connect(*args,**kwargs):
        nonlocal count
        count+=1
        return line if count==1 else bot
    monkeypatch.setattr('websockets.asyncio.client.connect',connect)
    await call.run()
    row=stats(call)
    segments=[l for l in row['lines'] if l['role']=='bot_rep']
    assert len(segments)==1 and segments[0]['ended_at'] is None
    assert segments[0]['last_sent_at']>=segments[0]['started_at']
    assert row['ended']['closing_line_completed'] is False


async def test_bot_abnormal_close_is_error_not_peer(local_call,monkeypatch):
    call,line=local_call
    call._agent_mode='sonic'
    call._bot_endpoint=call.dial.stream_url
    class Bot(Line):
        async def send(self,raw):pass
        def __aiter__(self):return self.stream()
        async def stream(self):
            if False:yield ''
            raise ConnectionClosedOK(Close(1000,''),Close(1000,''),True)
    bot=Bot();bot.call=call
    count=0
    async def connect(*args,**kwargs):
        nonlocal count
        count+=1
        return line if count==1 else bot
    monkeypatch.setattr('websockets.asyncio.client.connect',connect)
    with pytest.raises(Exception):await call.run()
    assert stats(call)['ended']['by']=='error'
    assert stats(call)['ended']['reason']=='bot_closed'


async def test_api_decision_during_send_never_records_later_instance(local_call,monkeypatch):
    import base64
    call,line=local_call
    monkeypatch.setattr(call,'_line_audio',lambda line: b'\xff'*160 if line.role=='ivr' else b'\x7f'*241)
    entered,release=asyncio.Event(),asyncio.Event()
    original=line.send
    count=0
    async def send(raw):
        nonlocal count
        if json.loads(raw)['event']=='media':
            count+=1
            if base64.b64decode(json.loads(raw)['media']['payload']).startswith(b'\x7f'):
                entered.set()
                await release.wait()
        await original(raw)
    line.send=send
    task=asyncio.create_task(call.run())
    await entered.wait()
    hangup=asyncio.create_task(call.hangup(source='api'))
    await asyncio.sleep(0)
    assert call._ended_fact is not None
    release.set()
    await asyncio.gather(task,hangup)
    row=stats(call)
    assert row['rep_first_audio_at'] is None
    assert all(l['started_at'] <= row['ended']['at'] for l in row['lines'])


async def test_window_straddling_rep_start_keeps_whole_arrival_span(local_call):
    call,_=local_call
    call._rep_first_audio_at=10.
    call._observe_inbound(VOICE[:160],9.99)
    call._observe_inbound(VOICE[160:],10.01)
    await call.hangup(source='api')
    assert stats(call)['nonsilent_inbound_spans_during_rep']==[[9.99,10.01]]


async def test_received_packet_is_observed_before_a_blocked_send_can_end(local_call):
    import base64
    from tests.company._signals import pipecat_dtmf_pcm,ulaw_roundtrip
    call,line=local_call
    entered,release=asyncio.Event(),asyncio.Event()
    original=line.send
    count=0
    async def send(raw):
        nonlocal count
        if json.loads(raw)['event']=='media':
            count+=1
            if call._rep_first_audio_at is not None:
                entered.set()
                await release.wait()
        await original(raw)
    line.send=send
    task=asyncio.create_task(call.run())
    await entered.wait()
    tone=ulaw_roundtrip(await pipecat_dtmf_pcm('0'))[:320]
    await line.incoming.put(json.dumps({'event':'media','streamSid':call._stream_sid,
        'media':{'payload':base64.b64encode(ulaw_encode(tone)).decode()}}))
    await asyncio.sleep(.02)
    hangup=asyncio.create_task(call.hangup(source='api'))
    await asyncio.sleep(0)
    release.set()
    await asyncio.gather(task,hangup)
    row=stats(call)
    assert len(row['dtmf_tones'])==1 and row['dtmf_tones'][0]['digits']==0
    assert row['nonsilent_inbound_frames_during_rep']==1


async def test_replaying_same_line_object_opens_a_new_instance(local_call):
    from waitless.models import CompanyLine
    call,_=local_call
    line=CompanyLine(step_id='greeting',kind='prompt',role='ivr',text='Local fixture.')
    call._record_line_sent(line,10.,10.02,new_playback=True)
    call._record_line_sent(line,11.,11.02,new_playback=True)
    await call.hangup(source='api')
    lines=stats(call)['lines']
    assert [l['started_at'] for l in lines]==[10.,11.]
    assert [l['ended_at'] for l in lines]==[10.02,11.02]


async def test_bank_baseline_closing_line_finishes_on_the_wire(local_call,company_dir):
    call,_=local_call
    bank=load_company(company_dir,'bluefin-harbor-bank')
    scenario=load_scenario(company_dir,bank.company_id,'baseline')
    closing=scenario.steps['rep'].text_script[-1]
    assert closing.expect.intent=='confirm_and_end' and closing.expect.outcome=='needs_follow_up'
    rep=scenario.steps['rep'].model_copy(update={'text_script':[closing]})
    call.company=bank
    call.dial=call.dial.model_copy(update={'to_number':bank.line_number})
    call._scenario=scenario.model_copy(update={'steps':{'greeting':scenario.steps['greeting'].model_copy(update={'next':'rep'}),'rep':rep}})
    await asyncio.wait_for(call.run(),.5)
    row=stats(call)
    assert row['company_id']==bank.company_id and row['ended']['by']=='company'
    assert row['ended']['reason']=='scenario_end' and row['ended']['closing_line_completed']
    line=row['lines'][row['ended']['closing_index']]
    assert (line['role'],line['intent'],line['outcome'])==('rep','confirm_and_end','needs_follow_up')

