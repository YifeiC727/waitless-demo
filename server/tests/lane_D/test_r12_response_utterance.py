"""Response FINAL blocks are recorded together, independent of speech permissions."""
import pytest
from pipecat.frames.frames import LLMFullResponseStartFrame, LLMFullResponseEndFrame
from fakes import SonicResponse
from lane_D.agent_support import *
from waitless.models import ProviderTextEvent

CHUNKS = ['One moment. ', 'I am an AI assistant calling for Alex. ', 'How can we proceed?']

async def text(h, stage, words, number, stop='PARTIAL_TURN'):
    await h.call._tap.on_provider_text(ProviderTextEvent(role='ASSISTANT', stage=stage, text=words,
        response=number, stop_reason=stop, t_ms=h.clock.monotonic_ms()))
    await settle(100)

async def multi_chunk(h, chunks):
    original = h.sonic._text_event
    async def hold_final(role, stage, words, stop, response):
        if stage != 'FINAL':
            await original(role, stage, words, stop, response)
    h.sonic._text_event = hold_final
    try:
        number = await h.sonic.emit_response(SonicResponse(text=chunks[0], audio_ms=200, stop_reason='PARTIAL_TURN'))
    finally:
        h.sonic._text_event = original
    await settle(100)
    for chunk in chunks[1:]:
        await text(h, 'SPECULATIVE', chunk, number)
    return number

@pytest.mark.parametrize('action', ['handoff', 'end'])
async def test_r12_announcement_waits_for_last_final(agent_call, action):
    h = agent_call
    await h.boot()
    n = await multi_chunk(h, CHUNKS[:2])
    await text(h, 'FINAL', CHUNKS[0], n)
    assert not h.events('waitless_utterance')
    await text(h, 'FINAL', CHUNKS[1], n, 'END_TURN')
    assert [e.transcript for e in h.events('waitless_utterance')] == [''.join(CHUNKS[:2])]
    if action == 'handoff':
        await h.tool('request_handoff', {'reason': 'Verification'})
        await h.clock.advance(h.settings.engine.handoff.announce_idle_ms + 10)
        await settle(200)
        assert h.events('handoff_announced')[0].action['text'] == ''.join(CHUNKS[:2])
    else:
        await h.tool('end_call', {'outcome': 'needs_follow_up', 'summary': 'Follow up'})
        await h.clock.advance(9000)
        await wait_until(lambda: h.on_ended.calls)
        assert h.events('hangup')
    h.assert_trace_contract()

@pytest.mark.parametrize('stop_control', [True, False])
async def test_r12_interruption_completes_received_blocks(agent_call, stop_control):
    h = agent_call
    await h.boot()
    n = await multi_chunk(h, CHUNKS[:2])
    if stop_control:
        await text(h, 'FINAL', CHUNKS[0], n)
        assert not h.events('waitless_utterance')
        await text(h, 'FINAL', '{ "interrupted" : true }', n)
    else:
        await text(h, 'FINAL', CHUNKS[0], n, 'INTERRUPTED')
    await text(h, 'FINAL', CHUNKS[1], n)
    assert [e.transcript for e in h.events('waitless_utterance')] == [CHUNKS[0]]

async def test_r12_timeout_starts_at_response_end(agent_call):
    h = agent_call
    await h.boot()
    n = await multi_chunk(h, CHUNKS[:2])
    await text(h, 'FINAL', CHUNKS[0], n)
    assert not h.events('waitless_utterance')
    await h.clock.advance(h.settings.engine.guard.held_response_max_ms - 1)
    assert not h.events('waitless_utterance')
    await h.clock.advance(1)
    await text(h, 'FINAL', CHUNKS[1], n)
    assert [e.transcript for e in h.events('waitless_utterance')] == [CHUNKS[0]]

async def test_r12_newer_final_completes_older_with_its_own_timestamp(agent_call):
    h = agent_call
    await h.boot()
    n = await multi_chunk(h, CHUNKS[:2])
    await text(h, 'FINAL', CHUNKS[0], n)
    before = h.clock.monotonic_ms()
    await h.clock.advance(20)
    await h.sonic_says('New response.')
    assert [e.transcript for e in h.events('waitless_utterance')] == [CHUNKS[0], 'New response.']
    assert h.events('waitless_utterance')[0].t_ms == before

async def test_r12_final_before_end_completes_at_end(agent_call):
    h = agent_call
    await h.boot()
    await h.sonic.push_frame(LLMFullResponseStartFrame())
    await settle(100)
    await text(h, 'SPECULATIVE', CHUNKS[0], 1)
    await text(h, 'FINAL', CHUNKS[0], 1)
    assert not h.events('waitless_utterance')
    await h.sonic.push_frame(LLMFullResponseEndFrame())
    await settle(200)
    assert [e.transcript for e in h.events('waitless_utterance')] == [CHUNKS[0]]

async def test_r12_pending_response_dropped_across_handoff(agent_call):
    h = agent_call
    await h.boot()
    n = await multi_chunk(h, CHUNKS[:2])
    await text(h, 'FINAL', CHUNKS[0], n)
    await h.tool('request_handoff', {'reason': 'Verification'})
    await h.clock.advance(h.settings.engine.handoff.announce_idle_ms + 10)
    await settle(200)
    joined = await h.call.join()
    await h.call.hangup('user_hangup')
    await text(h, 'FINAL', CHUNKS[1], n)
    assert not h.events('waitless_utterance')

async def test_r12_single_block_guardrail(agent_call):
    h = agent_call
    await h.boot()
    await h.sonic_says('I am an AI assistant calling on behalf of Alex.')
    assert [e.transcript for e in h.events('waitless_utterance')] == ['I am an AI assistant calling on behalf of Alex.']
