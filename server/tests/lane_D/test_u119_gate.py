"""v5.27 permission and real 1.12.0 wire cleanup regressions (offline)."""
import asyncio
import audioop
import base64
import json
import struct
from types import SimpleNamespace

import pytest
from pipecat.frames.frames import InterruptionFrame, LLMFullResponseEndFrame, TTSAudioRawFrame
from pipecat.processors.frame_processor import FrameDirection
from pipecat.serializers.twilio import TwilioFrameSerializer
from pipecat.transports.websocket.fastapi import FastAPIWebsocketParams
from call_engine._support import Chain, settle
from waitless.call.line_ws import LineSerializer
from waitless.call.media_gate import MediaGate


def audio(identity=(1, 1), seq=1, ms=20, value=b'\x11\x11'):
    f = TTSAudioRawFrame(audio=value * (16 * ms), sample_rate=16000, num_channels=1)
    f.response_identity, f.company_seq = identity, seq
    return f


async def pending(gate, seq):
    gate.begin_pending(seq)
    await gate.clear_output()


async def test_initial_closed_and_control_frames_flow():
    g = MediaGate()
    async with Chain(g.outbound) as c:
        await c.push(audio())
        await c.push(LLMFullResponseEndFrame())
        assert not c.tail.tts_audio()
        assert c.tail.down(LLMFullResponseEndFrame)
        assert g.output_state == 'closed'


async def test_pending_allowed_hold_and_new_response():
    g = MediaGate()
    async with Chain(g.outbound) as c:
        await pending(g, 1)
        g.bind_response((1, 1))
        await c.push(audio())
        assert not c.tail.tts_audio()
        assert await g.resolve_speaker(1, 'virtual_agent')
        assert len(c.tail.tts_audio()) == 1
        await pending(g, 2)
        g.bind_response((1, 2))
        await c.push(audio((1, 2), 2))
        await g.resolve_speaker(2, 'hold')
        assert g.output_state == 'hold' and not g.in_handoff
        await pending(g, 3)
        g.bind_response((1, 3))
        await c.push(audio((1, 1), 1))
        await c.push(audio((1, 2), 2))
        await c.push(audio((1, 3), 3))
        await g.resolve_speaker(3, 'menu_or_recording')
        assert [f.response_identity for f in c.tail.tts_audio()] == [(1, 1), (1, 3)]
        assert not await g.resolve_speaker(2, 'virtual_agent')
        await g.enter_handoff()
        await c.push(audio((1, 3), 3))
        await g.end_output()
        assert g.output_state == 'ended' and len(c.tail.tts_audio()) == 2


async def test_buffer_overflow_revokes_entire_response_including_tail():
    g = MediaGate(output_buffer_ms=30)
    async with Chain(g.outbound) as c:
        await pending(g, 1)
        g.bind_response((1, 1))
        await c.push(audio(ms=20))
        await c.push(audio(ms=20))
        await g.resolve_speaker(1, 'virtual_agent')
        await c.push(audio(ms=10))
        assert not c.tail.tts_audio()
        assert not g.response_audible((1, 1))


async def test_clear_completion_is_a_barrier_for_permission_and_user_media():
    g = MediaGate()
    entered, release = asyncio.Event(), asyncio.Event()
    async def clean():
        entered.set()
        await release.wait()
    g.set_output_cleaner(clean)
    async with Chain(g.outbound) as c:
        g.begin_pending(1)
        cleanup = asyncio.create_task(g.clear_output())
        await entered.wait()
        g.bind_response((1, 1))
        await c.push(audio())
        verdict = asyncio.create_task(g.resolve_speaker(1, 'virtual_agent'))
        await settle()
        assert not verdict.done() and not c.tail.tts_audio()
        release.set()
        await cleanup
        await verdict
        assert len(c.tail.tts_audio()) == 1


async def test_swap_discards_old_response_and_pending_verdict():
    g = MediaGate()
    async with Chain(g.outbound) as c:
        await pending(g, 1)
        g.bind_response((1, 1))
        await c.push(audio())
        await g.invalidate_stream()
        assert not await g.resolve_speaker(1, 'virtual_agent')
        await c.push(audio())
        assert not c.tail.tts_audio() and g.output_state == 'closed'


async def noop(*args, **kwargs):
    pass


class MemoryClient:
    is_connected, is_closing = True, False
    setup = disconnect = noop
    def __init__(self):
        self.packets = []
        self.media_ready = asyncio.Event()
    async def send(self, payload):
        packet = json.loads(payload)
        self.packets.append(packet)
        if packet['event'] == 'media':
            self.media_ready.set()


def output_for(client):
    from waitless.call.output_transport import ClearingWebsocketOutputTransport
    serializer = LineSerializer(stream_sid='offline-stream',
        params=TwilioFrameSerializer.InputParams(sample_rate=16000, auto_hang_up=False))
    params = FastAPIWebsocketParams(audio_out_enabled=True, audio_out_sample_rate=16000,
        audio_in_sample_rate=16000, serializer=serializer, add_wav_header=False)
    return ClearingWebsocketOutputTransport(SimpleNamespace(cleanup=noop), client, params,
        enable_direct_mode=True)


def old_samples(packet):
    decoded = audioop.ulaw2lin(base64.b64decode(packet['media']['payload']), 2)
    return sum(3500 < s < 5500 for (s,) in struct.iter_unpack('<h', decoded))


@pytest.mark.parametrize('join', [False, True])
@pytest.mark.parametrize('old_rate', [16000, 24000])
async def test_real_sender_partial_pcm_and_filter_cleared_before_fresh_wire_audio(join, old_rate):
    client = MemoryClient()
    out = output_for(client)
    g = MediaGate()
    out.bind_gate(g)
    async with Chain(g.outbound, out) as c:
        await pending(g, 1)
        g.bind_response((1, 1))
        await g.resolve_speaker(1, 'virtual_agent')
        f = audio(ms=20)
        f.sample_rate = old_rate
        await c.push(f)
        sender = out._media_senders[None]
        assert not sender._bot_speaking
        if join:
            await g.enter_handoff()
            fresh = asyncio.create_task(g.outbound.push_user_audio(b'\x22\x22' * 6400))
        else:
            await pending(g, 2)
            g.bind_response((1, 2))
            await g.resolve_speaker(2, 'virtual_agent')
            fresh = asyncio.create_task(c.push(audio((1, 2), 2, ms=400, value=b'\x22\x22')))
        await asyncio.wait_for(client.media_ready.wait(), 3)
        await fresh
        clear = max(i for i, p in enumerate(client.packets) if p['event'] == 'clear')
        packets = [p for p in client.packets[clear+1:] if p['event'] == 'media']
        assert packets and all(old_samples(p) == 0 for p in packets)
        print(f'WIRE join={join} old_rate={old_rate} old_samples_after_clear=0')


async def test_serializer_discards_absorbed_history_before_clear():
    client = MemoryClient()
    out = output_for(client)
    async with Chain(out):
        serializer = out._params.serializer
        assert await serializer.serialize(audio(ms=40)) is None
        clear = json.loads(await serializer.serialize(InterruptionFrame()))
        assert clear['event'] == 'clear'
        packet = json.loads(await serializer.serialize(audio(ms=200, value=b'\x22\x22')))
        assert old_samples(packet) == 0


async def test_superseded_cleanup_wakes_old_verdict_without_releasing_it():
    g = MediaGate()
    g.begin_pending(1)
    old = asyncio.create_task(g.resolve_speaker(1, 'virtual_agent'))
    await settle()
    g.begin_pending(2)
    await g.clear_output()
    assert await asyncio.wait_for(old, .2) is False
    assert g.output_state == 'pending' and g.company_seq == 2


async def test_queued_keypad_frame_keeps_its_original_epoch():
    from pipecat.frames.frames import OutputDTMFUrgentFrame
    g = MediaGate()
    async with Chain(g.outbound) as c:
        await pending(g, 1)
        await g.resolve_speaker(1, 'virtual_agent')
        old = OutputDTMFUrgentFrame.from_string('1')
        old._waitless_output_epoch = g.output_epoch
        await pending(g, 2)
        await g.resolve_speaker(2, 'virtual_agent')
        await c.push(old)
        assert not c.tail.dtmf()


async def test_permitted_half_block_is_not_yet_a_spoken_utterance():
    client = MemoryClient()
    out = output_for(client)
    g = MediaGate()
    out.bind_gate(g)
    async with Chain(g.outbound, out) as c:
        await pending(g, 1)
        g.bind_response((1, 1))
        await g.resolve_speaker(1, 'virtual_agent')
        await c.push(audio(ms=20))
        await settle()
        assert not any(p['event'] == 'media' for p in client.packets)
        assert not g.response_audible((1, 1))


async def test_wire_ack_preserves_response_identity_after_sender_slices_pcm():
    from pipecat.frames.frames import LLMFullResponseStartFrame
    client = MemoryClient()
    out = output_for(client)
    g = MediaGate()
    out.bind_gate(g)
    seen = []
    async def audible(identity):
        seen.append(identity)
    g.output_observers.append(audible)
    async with Chain(g.outbound, out) as c:
        await pending(g, 1)
        g.bind_response((4, 12))
        await g.resolve_speaker(1, 'virtual_agent')
        start = LLMFullResponseStartFrame()
        start.response_identity = (4, 12)
        await c.push(start)
        await c.push(audio((4, 12), ms=400))
        await asyncio.wait_for(client.media_ready.wait(), 3)
        await settle()
        assert seen and set(seen) == {(4, 12)}
        assert g.response_audible((4, 12))
