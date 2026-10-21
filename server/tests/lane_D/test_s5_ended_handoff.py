"""D-s5: a stale Return cannot resume a finished carrier call."""

import pytest

from call_engine.conftest import compiled, deps, engine, gate, line_number, settings, spec, store, trace
from call_engine._support import join_handoff,  settle
from call_engine.test_resume import harness, RESUME
from waitless.interfaces import ToolFailure


async def test_return_after_company_hangup_has_no_resume_side_effects(
    monkeypatch, settings, spec, fake_clock, trace, gate, make_fake_sonic, fake_carrier, store, engine,
):
    h = harness(monkeypatch, settings, spec, fake_clock, trace, gate, make_fake_sonic, fake_carrier, store)
    try:
        await reach_handoff_ready(h, engine)
        join, ws = await join_handoff(h)
        await h.call.hangup("company_hangup")
        await settle()
        before = trace.events(call_id=spec.call_id)
        with pytest.raises(ToolFailure) as info:
            await h.call.return_to_agent(join.handoff_id)
        assert info.value.code == "no_active_handoff"
        assert trace.events(call_id=spec.call_id) == before
        assert RESUME not in h.sonic.sent_texts
        assert h.call.status().phase == "ended"
        assert {"type": "ended", "reason": "company_hangup"} in ws.sent_json()
    finally:
        await h.close()

from lane_D.agent_support import reach_handoff_ready
