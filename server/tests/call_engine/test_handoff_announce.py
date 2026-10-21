"""AC-77: agent speech precedes tool-driven handoff, without template injection."""
import asyncio
import pytest
from lane_D.agent_support import *
from waitless.interfaces import ToolFailure
from call_engine._support import first_seq, audio_bytes, response_audio


async def test_ac77_announcement_is_spoken_before_the_handoff_starts(agent_call):
    h = agent_call
    await h.boot()
    with pytest.raises(ToolFailure) as exc:
        await h.call.join()
    assert exc.value.code == 'handoff_not_ready'
    await h.sonic_says('I will bring the account holder onto the call.')
    task = asyncio.create_task(invoke_tool(h.sonic, 'request_handoff', {'reason': 'Verification'}))
    await settle(100)
    idle = h.settings.engine.handoff.announce_idle_ms
    await h.clock.advance(idle - 1)
    assert h.call.status().phase != 'handoff_ready'
    assert not h.events('handoff_started')
    await h.clock.advance(1)
    await task
    join = await h.call.join()
    assert join.company_name == h.spec.company_name
    assert audio_bytes(h.transport.output().tts_audio()) == response_audio(h.sonic, 1)
    assert [e.action['text'] for e in h.events('handoff_announced')] == ['I will bring the account holder onto the call.']
    h.assert_trace_contract()


async def test_ac77_backend_writes_the_handoff_events_in_order(agent_call):
    h = agent_call
    await h.boot()
    await h.sonic_says('The account holder can verify that.')
    await h.tool('request_handoff', {'reason': 'Verification'})
    await h.clock.advance(h.settings.engine.handoff.announce_idle_ms + 10)
    await settle(200)
    seq = [first_seq(h.trace, h.spec.call_id, kind) for kind in ('handoff_announced', 'handoff_ready', 'handoff_started')]
    assert seq == sorted(seq) and len(set(seq)) == 3
    assert h.call._media_gate.in_handoff
    assert not h.events('handoff_ended')
    h.assert_trace_contract()
