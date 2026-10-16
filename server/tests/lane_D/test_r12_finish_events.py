"""Agent hangup is durable before cancellation and the independent result callback."""
import asyncio
import pytest
from lane_D.agent_support import *


def ending_events(h):
    return [e for e in h.events() if e.event_type in ('hangup', 'remote_hangup', 'call_ended')]


def capture_finish(h):
    snapshots = []
    original = h.call.deps.on_ended
    async def on_ended(call_id):
        snapshots.append([e.event_type for e in ending_events(h)])
        await original(call_id)
    h.call.deps.on_ended = on_ended
    return snapshots


@pytest.mark.parametrize('disconnect_during_request', [True, False])
async def test_r12_end_writes_hangup_then_ended_before_results(agent_call, monkeypatch, disconnect_during_request):
    h = agent_call
    snapshots = capture_finish(h)
    await h.boot()
    if disconnect_during_request:
        async def hangup(call_sid):
            await h.call._on_line_disconnected(None, None)
            await asyncio.Event().wait()
        monkeypatch.setattr(h.call.carrier, 'hangup', hangup)
    assert await h.tool('end_call', {'outcome': 'confirmed', 'confirmation_number': 'LE-12345', 'summary': 'Done'}) == {'ok': True}
    await h.clock.advance(9000)
    await wait_until(lambda: snapshots)
    assert [e.event_type for e in ending_events(h)] == ['hangup', 'call_ended']
    assert ending_events(h)[-1].action == {'reason': 'hangup'}
    assert snapshots == [['hangup', 'call_ended']]
    assert h.on_ended.calls == [h.spec.call_id]
    h.assert_trace_contract()
    await h.call._finish(h.spec.call_id)
    await h.call._on_line_disconnected(None, None)
    assert len(ending_events(h)) == 2 and len(h.on_ended.calls) == 1


async def test_r12_remote_disconnect_records_company_hangup(agent_call):
    h = agent_call
    snapshots = capture_finish(h)
    await h.boot()
    await h.call._on_line_disconnected(None, None)
    await wait_until(lambda: snapshots)
    assert [e.event_type for e in ending_events(h)] == ['remote_hangup', 'call_ended']
    assert ending_events(h)[-1].action == {'reason': 'company_hangup'}
    assert not h.carrier.hangups


async def test_r12_shutdown_records_explicit_reason_without_hangup(agent_call, monkeypatch):
    h = agent_call
    snapshots = capture_finish(h)
    await h.boot()
    async def hangup(call_sid):
        await h.call._on_line_disconnected(None, None)
        await settle()
    monkeypatch.setattr(h.call.carrier, 'hangup', hangup)
    await h.call.hangup('shutdown')
    assert [e.event_type for e in ending_events(h)] == ['call_ended']
    assert ending_events(h)[0].action == {'reason': 'shutdown'}
    assert snapshots == [['call_ended']]


async def test_r12_finish_reentry_guardrail(agent_call):
    h = agent_call
    snapshots = capture_finish(h)
    await h.boot()
    await asyncio.gather(h.call._finish(h.spec.call_id), h.call._finish(h.spec.call_id))
    assert h.on_ended.calls == [h.spec.call_id]
    assert snapshots == [['call_ended']]
