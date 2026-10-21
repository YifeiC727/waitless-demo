"""Real helper forced promotion and distinct receive sources, no provider IO."""
import asyncio
import base64
import json
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from pipecat.frames.frames import OutputAudioRawFrame
from pipecat.services.aws.nova_sonic.llm import AWSNovaSonicLLMService
from pipecat.services.aws.nova_sonic.session_continuation import SessionContinuationHelper
from aws_sdk_bedrock_runtime.models import BidirectionalOutputPayloadPart, InvokeModelWithBidirectionalStreamOutputChunk
from call_engine._support import settle
from lane_D.test_u115_repeat_hold import repeat_call, settings, spec, engine, compiled, line_number, store
from waitless.call.media_gate import MediaGate


async def prepare(h):
    s = h.sonic
    s._prompt_name, s._input_audio_content_name = 'old-prompt', 'old-audio'
    s._receive_task = None
    s._client = None
    s._audio_input_started = True
    s.send_audio_input_start = AsyncMock()
    s._sc.close_old_session = AsyncMock()
    s.create_task = lambda coro, **kw: h.tasks.append(asyncio.create_task(coro)) or h.tasks[-1]
    s._receive_task_handler = AsyncMock()
    return s


@pytest.mark.parametrize('failure', [False, True])
async def test_real_forced_monitor_promotes_service_and_cleans_old_stream(repeat_call, monkeypatch, failure):
    import pipecat.services.aws.nova_sonic.session_continuation as sc_module
    h = repeat_call
    s = await prepare(h)
    helper = s._sc
    old = s._stream
    helper._connected_time = 1
    helper._waiting_for_audio_start = True
    helper._audio_start_wait_time = 1
    helper._wants_connection = True
    helper._params.audio_start_timeout_seconds = 1
    if failure:
        helper._prepare_next_session = AsyncMock(side_effect=RuntimeError('offline prepare failed'))
    else:
        helper._next_session = SimpleNamespace(client=None, stream=object(), prompt_name='new-prompt', input_audio_content_name='new-audio')
        helper._next_session_created_time = 999
    real_sleep = asyncio.sleep
    async def tick(seconds):
        helper._wants_connection = False
        await real_sleep(0)
    monkeypatch.setattr(sc_module, 'time', SimpleNamespace(time=lambda: 1000))
    monkeypatch.setattr(sc_module.asyncio, 'sleep', tick)
    await helper._monitor_loop()
    for _ in range(3):
        await asyncio.gather(*list(h.tasks))
    if failure:
        assert s._stream is old and s._prompt_name == 'old-prompt'
        assert not helper._is_buffering
    else:
        assert s._prompt_name == 'new-prompt', f'service_still={s._prompt_name}'
        assert s._stream is not old and s._audio_input_started
        assert helper.close_old_session.await_args.args[1] is old
    assert helper.execute_handoff.__func__ is SessionContinuationHelper.execute_handoff


async def test_normal_and_forced_concurrent_then_repeated_swap(repeat_call):
    h = repeat_call
    s = await prepare(h)
    for number in range(2):
        old = s._stream
        s._sc._next_session = SimpleNamespace(client=None, stream=object(), prompt_name=f'new-{number}', input_audio_content_name='audio')
        forced = s._sc._create_task(s._sc.execute_handoff())
        await asyncio.gather(s._run_sc_handoff(), forced)
        assert s._prompt_name == f'new-{number}' and s._stream is not old
        assert s._sc.close_old_session.await_count == number + 1
    await asyncio.gather(*h.tasks)
    await s._sc.cleanup_next_session()
    assert not s._sc.handoff_in_progress and not s._sc._next_session


class ReceiveStream:
    def __init__(self):
        self.queue = asyncio.Queue()
    async def await_output(self):
        return None, self
    async def receive(self):
        return await self.queue.get()
    def emit(self, event):
        self.queue.put_nowait(InvokeModelWithBidirectionalStreamOutputChunk(
            value=BidirectionalOutputPayloadPart(bytes_=json.dumps({'event': event}).encode())))


async def test_two_real_receive_sources_reject_old_events_before_shared_state(repeat_call, monkeypatch):
    s, h = repeat_call.sonic, repeat_call
    gate = MediaGate()
    s.bind_media_gate(gate)
    got = []
    monkeypatch.setattr(AWSNovaSonicLLMService, 'push_frame', AsyncMock(side_effect=lambda f, *a, **kw: got.append(f)))
    s._report_user_transcription_ended = AsyncMock()
    s._report_assistant_response_started_base = None
    s._sc.on_assistant_audio_started = AsyncMock()
    first, second = ReceiveStream(), ReceiveStream()
    s._stream = first
    gate.begin_pending(1)
    await gate.clear_output()
    one = asyncio.create_task(s._receive_task_handler(stream=first))
    h.tasks.append(one)
    def start():
        return {'contentStart': {'type': 'TEXT', 'role': 'ASSISTANT',
            'additionalModelFields': json.dumps({'generationStage': 'SPECULATIVE'})}}
    first.emit(start())
    await settle(100)
    assert s._waitless_response == 1
    old_identity = s._waitless_response_identity
    s._stream = second
    two = asyncio.create_task(s._receive_task_handler(stream=second))
    h.tasks.append(two)
    await settle(100)
    gate.begin_pending(2)
    await gate.clear_output()
    first.emit({'contentStart': {'type': 'TEXT', 'role': 'USER'}})
    first.emit({'textOutput': {'content': 'stale private text'}})
    first.emit({'contentEnd': {'stopReason': 'END_TURN'}})
    first.emit({'audioOutput': {'content': base64.b64encode(b'\x11\x11'*480).decode()}})
    second.emit(start())
    second.emit({'audioOutput': {'content': base64.b64encode(b'\x22\x22'*480).decode()}})
    await settle(200)
    assert one.done()
    audios = [f for f in got if isinstance(f, OutputAudioRawFrame)]
    assert len(audios) == 1
    assert audios[0].response_identity[0] != old_identity[0]
    assert audios[0].company_seq == 2
    assert audios[0].audio == b'\x22\x22'*480
    assert 'stale private text' not in str(s._content_being_received)
    two.cancel()
    await asyncio.gather(two, return_exceptions=True)
