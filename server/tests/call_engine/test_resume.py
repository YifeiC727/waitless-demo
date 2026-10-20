"""AC-78 / D67: live disconnects stay isolated and never resume Sonic."""
from typing import Any
import pytest
from call_engine._support import handoff_sonic, join_handoff, patch_menu_parser, settle, wait_until
from waitless.call.agent_tools import event_text
from waitless.interfaces import ToolFailure
from lane_D.agent_support import AgentCallHarness as LiveCallHarness, reach_handoff_ready

RESUME = event_text({'event': 'handoff_ended'})


def harness(monkeypatch: Any, settings: Any, spec: Any, fake_clock: Any, trace: Any, gate: Any,
            make_fake_sonic: Any, fake_carrier: Any, store: Any) -> LiveCallHarness:
    patch_menu_parser(monkeypatch)
    return LiveCallHarness(monkeypatch=monkeypatch, settings=settings, spec=spec, clock=fake_clock,
        trace=trace, gate=gate, sonic=handoff_sonic(make_fake_sonic), carrier=fake_carrier, store=store)


async def test_ac78_live_return_is_refused_and_user_can_hang_up(
    monkeypatch, settings, spec, fake_clock, trace, gate, make_fake_sonic, fake_carrier, store, engine,
):
    h = harness(monkeypatch, settings, spec, fake_clock, trace, gate, make_fake_sonic, fake_carrier, store)
    try:
        await reach_handoff_ready(h, engine)
        join, ws = await join_handoff(h)
        before = h.sonic.input_frames - h.sonic.input_zero_frames
        with pytest.raises(ToolFailure) as exc:
            await h.call.return_to_agent(join.handoff_id)
        assert exc.value.code == 'no_active_handoff'
        await h.speech(200)
        assert h.sonic.input_frames - h.sonic.input_zero_frames == before
        await h.call.hangup('user_hangup')
        await settle(200)
        assert ws.closed and {'type': 'ended', 'reason': 'user_hangup'} in ws.sent_json()
        assert h.call.ended
        assert RESUME not in h.sonic.sent_texts
        assert not trace.of_type('handoff_ended', 'resume_injected', call_id=spec.call_id)
        assert trace.violations == []
    finally:
        await h.close()


async def test_ac78_socket_drop_expires_after_rejoin_grace_without_resume(
    monkeypatch, settings, spec, fake_clock, trace, gate, make_fake_sonic, fake_carrier, store, engine,
):
    h = harness(monkeypatch, settings, spec, fake_clock, trace, gate, make_fake_sonic, fake_carrier, store)
    try:
        await reach_handoff_ready(h, engine)
        _, ws = await join_handoff(h)
        ws.disconnect(1006)
        await settle(200)
        assert h.call.status().phase == 'handoff_ready'
        await h.clock.advance(engine.handoff.rejoin_grace_s * 1000 - 1)
        assert not h.call.ended and RESUME not in h.sonic.sent_texts
        await h.clock.advance(1)
        await settle(300)
        assert h.call.ended
        assert trace.of_type('call_ended', call_id=spec.call_id)[-1].action['reason'] == 'rejoin_timeout'
        assert not trace.of_type('handoff_ended', call_id=spec.call_id)
    finally:
        await h.close()


async def test_ac78_rejoin_during_grace_keeps_isolation_and_single_notification(
    monkeypatch, settings, spec, fake_clock, trace, gate, make_fake_sonic, fake_carrier, store, engine,
):
    h = harness(monkeypatch, settings, spec, fake_clock, trace, gate, make_fake_sonic, fake_carrier, store)
    try:
        await reach_handoff_ready(h, engine)
        _, ws = await join_handoff(h)
        ws.disconnect(1006)
        await settle(200)
        await h.clock.advance(engine.handoff.rejoin_grace_s * 500)
        await join_handoff(h)
        await h.clock.advance(engine.handoff.rejoin_grace_s * 1000)
        assert not h.call.ended and h.call.status().phase == 'in_handoff'
        assert RESUME not in h.sonic.sent_texts
        assert len(trace.of_type('handoff_ready', call_id=spec.call_id)) == 1
        assert not trace.of_type('handoff_ended', call_id=spec.call_id)
    finally:
        await h.close()
