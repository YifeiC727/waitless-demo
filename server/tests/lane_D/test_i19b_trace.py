"""Gate facts through real Trace -> SQLite Store -> Trace.events."""
import asyncio
import inspect
import json
from typing import get_args

import pytest
from waitless import models as m
from waitless.call.media_gate import MediaGate
from waitless.trace import Trace
from waitless.store import Store
from tests.call_engine._support import Chain, settle
from tests.lane_D.test_u119_gate import audio, pending, MemoryClient, output_for
from pipecat.frames.frames import OutputDTMFUrgentFrame, OutputAudioRawFrame

KINDS = {'output_revoked', 'output_allowed', 'output_cleared', 'response_bound', 'response_audio'}


@pytest.fixture
def facts(tmp_path, fake_clock):
    store = Store(tmp_path / 'facts.db')
    store.init()
    trace = Trace(store, fake_clock)
    def sink(kind, **fields):
        t = fields.pop('t_ms')
        trace.event(m.TraceEvent(task_id='task-facts', call_id='call-facts',
                                event_type=kind, t_ms=t, action=fields))
    yield trace, sink
    store.close()


def events(trace, kind):
    return [e for e in trace.events(call_id='call-facts') if e.event_type == kind]


def test_shared_event_contract():
    assert KINDS <= set(get_args(m.TraceEventType))
    assert KINDS <= set(m.TRACE_ACTION_KEYS)


async def test_real_gate_fact_order_and_once(facts, fake_clock):
    trace, sink = facts
    gate = MediaGate(clock=fake_clock, fact_sink=sink)
    assert not inspect.iscoroutinefunction(gate._fact)
    async with Chain(gate.outbound) as chain:
        await pending(gate, 1)
        gate.bind_response((1, 1))
        gate.bind_response((1, 1))
        await chain.push(audio())  # Buffered, never counted here.
        assert not events(trace, 'response_audio')
        await gate.resolve_speaker(1, 'virtual_agent')
        await chain.push(audio())
        gate.begin_pending(2)
        await gate.clear_output()
        await chain.push(audio())
        await gate.enter_handoff()
        await gate.end_output()
    assert len(events(trace, 'response_bound')) == 1
    summary, = events(trace, 'response_audio')
    assert summary.action == {'identity': '1:1', 'first_ms': 0, 'last_ms': 0, 'frames': 2}
    allowed, = events(trace, 'output_allowed')
    assert allowed.action == {'seq': 1, 'epoch': 1, 'speaker': 'virtual_agent'}
    revoke = next(e for e in events(trace, 'output_revoked') if e.action['trigger_seq'] == 2)
    assert revoke.action['revoked_seq'] == 1 and revoke.action['revoked_epoch'] == 1
    assert summary.action['last_ms'] <= revoke.t_ms
    encoded = json.dumps([e.action for e in trace.events()])
    assert all(word not in encoded for word in ['transcript', 'audio_bytes', 'text', 'keys', 'digits'])
    assert not events(trace, 'output_allowed')[1:]


@pytest.mark.parametrize('failure', [None, 'error', 'cancel'])
async def test_clear_completion_records_own_id_time_failure(facts, fake_clock, failure):
    trace, sink = facts
    gate = MediaGate(clock=fake_clock, fact_sink=sink)
    started, release = asyncio.Event(), asyncio.Event()
    async def cleaner():
        started.set()
        await release.wait()
        if failure == 'error':
            raise RuntimeError('controlled')
    gate.set_output_cleaner(cleaner)
    gate.begin_pending(1)
    task = asyncio.create_task(gate.clear_output())
    await started.wait()
    gate.begin_pending(2)
    fake_clock.advance_sync(80)
    if failure == 'cancel':
        task.cancel()
    else:
        release.set()
    await asyncio.gather(task, return_exceptions=True)
    cleared, = events(trace, 'output_cleared')
    assert cleared.action['clear_id'] == 1
    assert cleared.t_ms == 80 and cleared.action['ok'] is (failure is None)
    if failure is None:
        assert cleared.action['covers'] == []
        assert not gate._cleaned.is_set()  # Old cleanup cannot complete a newer revoke.
    else:
        assert not gate._cleaned.is_set()


async def test_superseded_cleanups_and_zero_frames(facts, fake_clock):
    trace, sink = facts
    gate = MediaGate(clock=fake_clock, fact_sink=sink)
    gate.begin_pending(1)
    gate.bind_response((3, 1))
    gate.bind_response((3, 2))
    gate.begin_pending(2)
    gate.begin_pending(3)
    await gate.clear_output()
    await gate.enter_handoff()
    await gate.end_output()
    summaries = events(trace, 'response_audio')
    assert {e.action['identity'] for e in summaries} == {'3:1', '3:2'}
    assert all(e.action['frames'] == 0 and e.action['first_ms'] is None and e.action['last_ms'] is None for e in summaries)
    cleared = events(trace, 'output_cleared')[0]
    assert cleared.action['clear_id'] == 3 and cleared.action['covers'] == [1,2]


async def test_overflow_settles_once_and_actual_first_after_r(facts, fake_clock):
    trace, sink = facts
    gate = MediaGate(clock=fake_clock, fact_sink=sink, output_buffer_ms=30)
    async with Chain(gate.outbound) as chain:
        await pending(gate, 1)
        gate.bind_response((1, 1))
        await chain.push(audio(ms=40))
        await gate.resolve_speaker(1, 'virtual_agent')
        gate.bind_response((1, 2))
        fake_clock.advance_sync(100)
        await chain.push(audio((1, 2), 1))
        await gate.end_output()
    rows = {e.action['identity']: e.action for e in events(trace, 'response_audio')}
    assert rows['1:1']['frames'] == 0
    assert rows['1:2']['first_ms'] == 100


@pytest.mark.parametrize('where', ['upstream', 'stamped', 'tail', 'serializer', 'send'])
async def test_real_transport_dtmf_handoff_barrier(where, engine_config, facts, fake_clock):
    trace, sink = facts
    client = MemoryClient()
    out = output_for(client)
    gate = MediaGate(clock=fake_clock, dtmf=engine_config.dtmf, fact_sink=sink)
    out.bind_gate(gate)
    async with Chain(gate.outbound, out) as chain:
        await pending(gate, 1)
        await gate.resolve_speaker(1, 'virtual_agent')
        frame = OutputDTMFUrgentFrame.from_string('0')
        entered, release = asyncio.Event(), asyncio.Event()
        if where in {'serializer', 'send'}:
            owner = out._params.serializer if where == 'serializer' else client
            method = 'serialize' if where == 'serializer' else 'send'
            original = getattr(owner, method)
            async def paused(value):
                if (where == 'serializer' and isinstance(value, OutputAudioRawFrame)) or (
                        where == 'send' and json.loads(value)['event'] == 'media'):
                    if not entered.is_set():
                        entered.set()
                        await release.wait()
                return await original(value)
            setattr(owner, method, paused)
            send = asyncio.create_task(chain.push(frame))
            await asyncio.wait_for(entered.wait(), 3)
            barrier = asyncio.create_task(gate.enter_handoff())
            await settle()
            release.set()
            await asyncio.gather(send, barrier)
        else:
            if where == 'stamped':
                frame._waitless_output_epoch = gate.output_epoch
                # Queue inside the actual fixed transport before revocation.
                await chain.push(frame)
            if where == 'tail':
                tail = OutputAudioRawFrame(audio=bytes(6400), sample_rate=16000, num_channels=1)
                tail._waitless_dtmf_tail = True
                tail._waitless_output_epoch = gate.output_epoch
                await chain.push(tail)
            await gate.enter_handoff()
            await chain.push(frame)
        if where == 'send':
            assert any(packet['event']=='media' for packet in client.packets)
        barrier_index = len(client.packets)
        await chain.push(frame)
        await asyncio.sleep(.1)
        assert not [p for p in client.packets[barrier_index:] if p['event'] == 'media']
        rev = next(e for e in events(trace, 'output_revoked') if e.action['state'] == 'handoff')
        clear = next(e for e in events(trace, 'output_cleared') if e.action['clear_id'] == rev.action['clear_id'])
        assert clear.action['ok'] is True and rev.t_ms <= clear.t_ms
        client.media_ready.clear()
        assert await gate.outbound.push_user_dtmf('1')
        await asyncio.wait_for(client.media_ready.wait(), 3)
        assert [p for p in client.packets[barrier_index:] if p['event'] == 'media']


async def test_observation_failure_propagates():
    def fail(*args, **kwargs):
        raise LookupError('trace unavailable')
    gate = MediaGate(fact_sink=fail)
    with pytest.raises(LookupError):
        gate.begin_pending(1)


async def test_pending_clear_discards_stamped_automatic_keys(facts):
    _, sink = facts
    client = MemoryClient()
    out = output_for(client)
    gate = MediaGate(fact_sink=sink)
    out.bind_gate(gate)
    async with Chain(gate.outbound, out) as chain:
        await pending(gate, 1)
        await gate.resolve_speaker(1, 'virtual_agent')
        frame = OutputDTMFUrgentFrame.from_string('0')
        frame._waitless_output_epoch = gate.output_epoch
        await chain.push(frame)
        await pending(gate, 2)
        count = len(client.packets)
        await out.process_frame(frame, __import__('pipecat.processors.frame_processor',fromlist=['FrameDirection']).FrameDirection.DOWNSTREAM)
        await asyncio.sleep(.1)
        assert not [p for p in client.packets[count:] if p['event']=='media']


from lane_D.test_u119_speaker import live119, agent_call, settings, spec, engine, compiled, line_number, store, trace, turn, reply


async def test_live_handoff_order_and_utterance_identity(live119):
    from waitless.models import ProviderTextEvent
    from pipecat.processors.frame_processor import FrameDirection
    h = live119
    await turn(h, reply())
    gate = h.call._media_gate
    gate.bind_response((5, 1))
    await gate.outbound.process_frame(audio((5, 1), 1), FrameDirection.DOWNSTREAM)
    ev = ProviderTextEvent(role='ASSISTANT', stage='FINAL', text='Local fixture.', response=1,
                           stop_reason='END_TURN', t_ms=h.clock.monotonic_ms())
    object.__setattr__(ev, 'response_identity', (5, 1))
    h.call._tap._write(ev)
    assert h.events('waitless_utterance')[-1].action['response_identity']=='5:1'
    await turn(h, reply('live_human'))
    rev = next(e for e in h.events('output_revoked') if e.action['state']=='handoff')
    clear = next(e for e in h.events('output_cleared') if e.action['clear_id']==rev.action['clear_id'])
    announced, ready = h.events('handoff_announced')[0], h.events('handoff_ready')[0]
    assert clear.action['ok'] and rev.seq < clear.seq < announced.seq < ready.seq
    assert rev.t_ms <= clear.t_ms <= announced.t_ms <= ready.t_ms
    # A late, zero-frame binding during teardown is still settled before ended.
    gate.bind_response((5, 2))
    await h.call.hangup('user_hangup')
    summaries = h.events('response_audio')
    assert {e.action['identity'] for e in summaries}=={'5:1','5:2'}
    assert all(e.seq < h.events('call_ended')[0].seq for e in summaries)


async def test_allowed_timestamp_is_actual_open_after_cleanup(facts,fake_clock):
    trace,sink=facts
    gate=MediaGate(clock=fake_clock,fact_sink=sink)
    entered,release=asyncio.Event(),asyncio.Event()
    async def cleaner():
        entered.set()
        await release.wait()
    gate.set_output_cleaner(cleaner)
    async with Chain(gate.outbound) as chain:
        gate.begin_pending(1)
        cleanup=asyncio.create_task(gate.clear_output())
        await entered.wait()
        gate.bind_response((1,1))
        await chain.push(audio())
        verdict=asyncio.create_task(gate.resolve_speaker(1,'virtual_agent'))
        await settle()
        assert not events(trace,'output_allowed')
        fake_clock.advance_sync(100)
        release.set()
        await cleanup
        await verdict
        assert events(trace,'output_allowed')[0].t_ms==100
        await gate.end_output()
        assert events(trace,'response_audio')[0].action['first_ms']==100
