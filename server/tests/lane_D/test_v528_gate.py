"""T5 real MediaGate ingress and synchronous output revocation."""
import asyncio
import inspect

import pytest
from pipecat.frames.frames import InputAudioRawFrame, OutputDTMFUrgentFrame
from pipecat.processors.frame_processor import FrameDirection
from pipecat.processors.frame_processor import FrameProcessor
from call_engine._support import Chain
from lane_D.test_u119_gate import audio, pending
from waitless.call.media_gate import MediaGate


@pytest.mark.parametrize('handoff', [False, True])
async def test_inbound_observers_get_original_before_zero_replacement(fake_clock, handoff):
    seen = []
    gate = MediaGate(clock=fake_clock)
    gate.inbound_observers.extend([
        lambda *args: seen.append(('first', args)),
        lambda *args: seen.append(('second', args)),
    ])
    original = b'\x10\x20' * 160
    async with Chain(gate.inbound) as chain:
        if handoff:
            await gate.enter_handoff()
        fake_clock.advance_sync(123)
        await chain.push(InputAudioRawFrame(audio=original, sample_rate=8000, num_channels=2))
        assert seen == [('first', (original, 8000, 2, 123)), ('second', (original, 8000, 2, 123))]
        downstream = chain.tail.down(InputAudioRawFrame)[-1]
        assert downstream.audio == (bytes(len(original)) if handoff else original)
        await chain.push(InputAudioRawFrame(audio=original, sample_rate=8000, num_channels=2), FrameDirection.UPSTREAM)
        assert len(seen) == 2
        gate.inbound_observers.clear()
        await chain.push(InputAudioRawFrame(audio=original, sample_rate=8000, num_channels=2))
        assert len(seen) == 2


async def test_revoke_closed_is_synchronous_and_defers_blocked_cleanup(fake_clock):
    facts = []
    gate = MediaGate(clock=fake_clock, fact_sink=lambda kind, **fields: facts.append((kind, fields)))
    async with Chain(gate.outbound) as chain:
        await pending(gate, 1)
        gate.bind_response((1, 1))
        await gate.resolve_speaker(1, 'virtual_agent')
        await chain.push(audio())
        chain.tail.clear()
        old_epoch = gate.output_epoch
        started, release = asyncio.Event(), asyncio.Event()
        async def cleaner():
            started.set()
            await release.wait()
        gate.set_output_cleaner(cleaner)
        assert not inspect.iscoroutinefunction(gate.revoke_closed)
        epoch = gate.revoke_closed()
        assert epoch == old_epoch + 1 == gate.output_epoch
        assert gate.company_seq == 1 and gate.output_state == 'closed'
        assert not gate.response_audible((1, 1)) and not started.is_set()
        revoked = [fields for kind, fields in facts if kind == 'output_revoked'][-1]
        assert (revoked['revoked_seq'], revoked['revoked_epoch'], revoked['new_epoch'], revoked['state']) == (1, old_epoch, epoch, 'closed')
        task = asyncio.create_task(gate.clear_output())
        await started.wait()
        assert not task.done()
        await chain.push(audio())
        await chain.push(OutputDTMFUrgentFrame.from_string('1'))
        assert not chain.tail.tts_audio() and not chain.tail.dtmf()
        release.set()
        await task
        assert gate.output_epoch == epoch and gate._cleaned.is_set()


@pytest.mark.parametrize('state', ['handoff', 'ended'])
async def test_revoke_closed_does_not_change_terminal_gate(state):
    facts = []
    gate = MediaGate(fact_sink=lambda kind, **fields: facts.append((kind, fields)))
    if state == 'handoff':
        await gate.enter_handoff()
    else:
        await gate.end_output()
    before = (gate.output_epoch, gate._cleaned, list(facts))
    assert gate.revoke_closed() == before[0]
    assert gate.output_state == state and gate._cleaned is before[1] and facts == before[2]


async def test_arrival_is_captured_before_processor_await(fake_clock, monkeypatch):
    gate = MediaGate(clock=fake_clock)
    seen = []
    gate.inbound_observers.append(lambda *args: seen.append(args[-1]))
    process = FrameProcessor.process_frame
    async def delayed(self, frame, direction):
        if isinstance(frame, InputAudioRawFrame):
            fake_clock.advance_sync(300)
        await process(self, frame, direction)
    monkeypatch.setattr(FrameProcessor, 'process_frame', delayed)
    async with Chain(gate.inbound) as chain:
        await chain.push(InputAudioRawFrame(audio=bytes(640), sample_rate=16000, num_channels=1))
    assert seen == [0]


async def test_end_output_clears_inbound_observers_before_cleanup_await():
    gate = MediaGate()
    gate.inbound_observers.append(lambda *args: None)
    started, release = asyncio.Event(), asyncio.Event()
    async def cleaner():
        started.set()
        await release.wait()
    gate.set_output_cleaner(cleaner)
    task = asyncio.create_task(gate.end_output())
    try:
        await started.wait()
        assert gate.output_state == 'ended' and gate.inbound_observers == []
    finally:
        release.set()
        await task
