import asyncio
import json
from types import SimpleNamespace
from unittest.mock import AsyncMock

from pipecat.adapters.services.aws_nova_sonic_adapter import AWSNovaSonicLLMAdapter
from pipecat.frames.frames import OutputAudioRawFrame, LLMFullResponseStartFrame, LLMFullResponseEndFrame
from pipecat.processors.frame_processor import FrameDirection
from pipecat.processors.aggregators.llm_context import LLMContext
from pipecat.services.aws.nova_sonic.llm import AWSNovaSonicLLMService
from call_engine._support import Chain, settle
from lane_D.test_u119_gate import MediaGate, audio, pending, output_for, MemoryClient
from lane_D.test_u119_speaker import live119, agent_call, settings, spec, engine, compiled, line_number, store, trace, turn, reply
from waitless.models import ProviderTextEvent


async def test_empty_tools_real_prompt_packet_has_no_tool_configuration():
    packets = []
    async def send(payload, stream):
        packets.append(json.loads(payload))
    service = SimpleNamespace(_audio_config=SimpleNamespace(output_sample_rate=24000,
        output_sample_size=16, output_channel_count=1), _settings=SimpleNamespace(voice='tiffany'), send_event=send)
    context = LLMContext(messages=[], tools=[])
    tools = AWSNovaSonicLLMAdapter().get_llm_invocation_params(context, system_instruction='offline')['tools'] or []
    await AWSNovaSonicLLMService.send_prompt_start(service, tools, 'offline', object())
    assert 'toolConfiguration' not in packets[-1]['event']['promptStart']


async def test_already_playing_queue_and_old_epoch_ingress_are_cleared():
    client = MemoryClient()
    out = output_for(client)
    g = MediaGate()
    out.bind_gate(g)
    async with Chain(g.outbound, out) as c:
        await pending(g, 1)
        g.bind_response((1, 1))
        await g.resolve_speaker(1, 'virtual_agent')
        await c.push(audio(ms=400))
        await asyncio.wait_for(client.media_ready.wait(), 3)
        sender = out._media_senders[None]
        assert sender._bot_speaking and sender._audio_queue.qsize()
        stale = audio(ms=400)
        stale._waitless_output_epoch = g.output_epoch
        await pending(g, 2)
        count = len(client.packets)
        await out.process_frame(stale, FrameDirection.DOWNSTREAM)
        await settle(200)
        assert len(client.packets) == count
        assert sender._buffered_audio_bytes == 0 and sender._audio_queue.qsize() == 0
        assert client.packets[-1]['event'] == 'clear'


async def test_unspoken_provider_final_is_not_an_utterance(live119):
    h = live119
    await turn(h, reply())
    gate = h.call._media_gate
    gate.bind_response((1, 1))
    ev = ProviderTextEvent(role='ASSISTANT', stage='SPECULATIVE', text='Unspoken', response=1,
        stop_reason='PARTIAL_TURN', t_ms=h.clock.monotonic_ms())
    object.__setattr__(ev, 'response_identity', (1, 1))
    await h.call._tap.on_provider_text(ev)
    await h.call._tap.on_response_end(1, h.clock.monotonic_ms())
    await h.call._tap.on_provider_text(ev.model_copy(update={'stage': 'FINAL', 'stop_reason': 'END_TURN'}))
    assert not h.events('waitless_utterance')
    frame = audio()
    await gate.outbound.process_frame(frame, FrameDirection.DOWNSTREAM)
    await settle()
    assert [e.transcript for e in h.events('waitless_utterance')] == ['Unspoken']


async def test_empty_latest_company_line_cannot_reuse_an_earlier_classification(live119):
    h = live119
    await turn(h, reply())
    gate = h.call._media_gate
    old_epoch = gate.output_epoch
    before = len(h.deps.jev.calls)
    policy_before = h.call._keypad.policy_state
    await turn(h, reply(), text='')
    assert h.call.status().phase == 'ai_agent' and not h.events('handoff_ready')
    assert gate.company_seq == 2 and gate.output_epoch > old_epoch
    assert gate.output_state != 'allowed'
    assert not h.deps.jev.calls  # the replacement client was not called
    assert before == 2 and h.call._keypad.policy_state == policy_before


async def test_hold_keeps_company_input_live(live119):
    h = live119
    await turn(h, reply('hold'))
    assert not h.call._media_gate.in_handoff
    before = h.sonic.input_frames - h.sonic.input_zero_frames
    await h.speech(100)
    assert h.sonic.input_frames - h.sonic.input_zero_frames > before
    await turn(h, reply('live_human'))
    assert h.call._media_gate.in_handoff


async def test_cancel_inflight_verdict_on_finish(live119):
    h = live119
    entered, cancelled = asyncio.Event(), asyncio.Event()
    async def ask(*args):
        entered.set()
        try:
            await asyncio.Event().wait()
        finally:
            cancelled.set()
    h.call._keypad.deps.jev = SimpleNamespace(ask=ask)
    await h.sonic.emit_user_text('Hello')
    await entered.wait()
    await h.call.hangup('user_hangup')
    assert cancelled.is_set()
    assert not h.events('decision') and not h.events('handoff_ready')


async def test_sync_revocation_stops_sender_before_cleanup_task_gets_cpu():
    client = MemoryClient()
    out = output_for(client)
    g = MediaGate()
    out.bind_gate(g)
    async with Chain(g.outbound, out) as c:
        await pending(g, 1)
        g.bind_response((1, 1))
        await g.resolve_speaker(1, 'virtual_agent')
        await c.push(audio(ms=400))
        await asyncio.wait_for(client.media_ready.wait(), 3)
        assert out._media_senders[None]._audio_queue.qsize() > 0
        g.begin_pending(2)
        before = len(client.packets)
        await asyncio.sleep(.09)
        assert len(client.packets) == before
        await g.clear_output()


async def test_sender_frame_already_sliced_cannot_write_after_sync_revocation(monkeypatch):
    client = MemoryClient()
    out = output_for(client)
    g = MediaGate()
    out.bind_gate(g)
    entered, release = asyncio.Event(), asyncio.Event()
    original = out.write_audio_frame
    async def delayed(frame):
        entered.set()
        await release.wait()
        return await original(frame)
    monkeypatch.setattr(out, 'write_audio_frame', delayed)
    async with Chain(g.outbound, out) as c:
        await pending(g, 1)
        g.bind_response((1, 1))
        await g.resolve_speaker(1, 'virtual_agent')
        await c.push(audio(ms=400))
        await entered.wait()
        g.begin_pending(2)
        before = len(client.packets)
        release.set()
        await asyncio.sleep(.09)
        assert len(client.packets) == before
        await g.clear_output()
