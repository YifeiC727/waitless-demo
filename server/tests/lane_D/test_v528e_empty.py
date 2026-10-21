"""13.2: empty guard FINALs own only audit records."""
import asyncio
import pytest
from call_engine._support import settle
from lane_D.test_v528d_requests import SplitWire
from lane_D.test_v528d_continuation import owner
from lane_v528c2.test_wiring import (live528, reply, turn, speaker_decisions,
    agent_call, settings, spec, engine, compiled, line_number, store, trace)

@pytest.mark.parametrize('text', ['', ' ', '[_ahhh]', 'Hello [name]'])
async def test_guard_empty_preserves_owner_marks_and_candidate(live528, text):
    h = live528; reply(h, transfer='pending'); await turn(h)
    reply(h, transfer='none'); await turn(h)
    before = owner(h); marks = dict(h.call._quiet.marks)
    candidate = h.call._quiet_candidate; token = h.call._quiet_token
    await turn(h, text)
    assert owner(h) == before
    assert h.call._quiet.marks == marks
    assert h.call._quiet_candidate is candidate and h.call._quiet_token == token
    d = speaker_decisions(h)[-1]
    assert d.action == {'turn_seq': 3} and d.policy_decision['no_jev']
    assert d.jev_response == {'answers': {}}
    assert d.jev_request['state']['transcript']['lines'] == []

async def test_guard_empty_does_not_expire_inflight_result(live528):
    h = live528; reply(h, transfer='pending'); await turn(h)
    wire = SplitWire(); h.call._keypad.deps.jev = wire
    await turn(h, 'Representative greeting')
    before = owner(h)
    await turn(h, '')
    assert owner(h) == before
    wire.release['main'].set(); wire.release['transfer'].set(); await settle(500)
    d = speaker_decisions(h)[-1]
    assert d.action == {'turn_seq': 2} and not d.policy_decision['stale']
