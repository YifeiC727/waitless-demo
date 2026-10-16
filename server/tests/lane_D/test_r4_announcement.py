"""Tool handoff and hangup wait for the last Sonic audio frame to become idle."""
import asyncio
import pytest
from lane_D.agent_support import *
from fakes import SonicResponse


@pytest.mark.parametrize('action', ['handoff', 'end'])
async def test_wait_for_sent_new_response_and_audio(agent_call, action):
    h = agent_call
    await h.boot()
    await h.sonic_says('I will bring the account holder in.' if action == 'handoff' else 'Goodbye.')
    tool = 'request_handoff' if action == 'handoff' else 'end_call'
    args = {'reason': 'Verify'} if action == 'handoff' else {'outcome': 'needs_follow_up', 'summary': 'Follow up'}
    task = asyncio.create_task(invoke_tool(h.sonic, tool, args))
    idle = h.settings.engine.handoff.announce_idle_ms
    await settle(100)
    await h.clock.advance(idle - 1)
    assert not h.events('handoff_ready') and not h.events('hangup')
    await h.sonic_says(SonicResponse(text='One more thing.', audio_ms=20))
    last = h.clock.monotonic_ms()
    await h.clock.advance(idle - 1)
    assert not h.events('handoff_ready') and not h.events('hangup')
    await h.clock.advance(1)
    await settle(200)
    await task
    kind = 'handoff_ready' if action == 'handoff' else 'hangup'
    assert h.events(kind)[0].t_ms - last == idle
    h.assert_trace_contract()
