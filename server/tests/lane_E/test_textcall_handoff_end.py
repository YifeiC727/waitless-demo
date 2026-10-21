"""Handoff termination must reach the browser and the call result lifecycle."""
import asyncio

import pytest

from e_support import (
    FakeHandoffSocket, ScriptedCompany, When, build_settings, line, make_rig,
    queue_reactions, run_until, spec_end, spec_handoff,
)
from waitless.interfaces import ToolFailure


async def handoff_rig(mode_env, engine_config, fake_clock, reactions):
    company = ScriptedCompany([line("The account holder must verify.", role="rep")], reactions)
    rig = make_rig(settings=build_settings(mode_env("text-sim"), engine_config),
                   clock=fake_clock, company=company, allowed_fields={},
                   script=[("must verify", spec_handoff()), ("Completed", spec_end())])
    session = await rig.start()
    await run_until(fake_clock, lambda: session.status().phase == "handoff_ready")
    return rig, session, company


def socket():
    ws = FakeHandoffSocket()
    ws.feed_json({"type": "hello", "format": "pcm_s16le", "sample_rate": 16000, "channels": 1})
    return ws


def assert_handoff_closed(rig, session, ws, reason):
    assert [frame["type"] for frame in ws.sent_json()] == ["ready", "ended"]
    assert ws.sent_json()[-1] == {"type": "ended", "reason": reason}
    assert ws.closed_code == 1000
    assert ws.sent_bytes() == []
    ended, = rig.events("handoff_ended")
    started, = rig.events("handoff_started")
    assert ended.action == {"dropped_sonic_outputs": 0}
    assert ended.decision_id == started.decision_id
    assert [e for e in rig.events() if started.seq < e.seq < ended.seq] == []
    assert session.status().phase == "ended" and session.status().ended
    assert session.status().handoff is None
    assert rig.ended_calls == [rig.spec.call_id]


@pytest.mark.parametrize("signal", ["hangup_line", "ended_flag"])
async def test_company_hangup_during_join_finishes_handoff(signal, mode_env, engine_config, fake_clock):
    def end_on_join(kind, value, company):
        if kind == "handoff" and value is True:
            if signal == "ended_flag":
                company._ended = True
                return []
            return [line("Private verification", role="rep"), line("Goodbye", kind="hangup")]
        return []

    rig, session, company = await handoff_rig(mode_env, engine_config, fake_clock, end_on_join)
    try:
        joined = await session.join()
        await run_until(fake_clock, lambda: session.ended, max_ms=2000, what="company hangup during join")
        await session.wait_ended()
        # A browser can connect after the synchronous text-company batch already ended.
        ws = socket()
        await asyncio.wait_for(session.attach_handoff(ws, joined.handoff_id), timeout=2)
        assert_handoff_closed(rig, session, ws, "company_hangup")
        assert len(rig.gate.round_calls) == 1
        assert all("Private verification" not in e.model_dump_json() for e in rig.events())
        assert len(rig.events("remote_hangup", "call_ended")) == 2
        assert rig.events("call_ended")[0].action == {"reason": "company_hangup"}
        with pytest.raises(ToolFailure) as err:
            await session.return_to_agent(joined.handoff_id)
        assert err.value.code == "no_active_handoff"
        with pytest.raises(ToolFailure):
            await session.attach_handoff(socket(), "another-handoff")
        assert company.handoffs() == [True]
    finally:
        await session.hangup("test cleanup")


@pytest.mark.parametrize("reason", ["company_hangup", "returned"])
async def test_connected_handoff_sends_reason_and_closes(reason, mode_env, engine_config, fake_clock):
    rig, session, company = await handoff_rig(
        mode_env, engine_config, fake_clock,
        queue_reactions(When("handoff", False, [line("Completed.", role="rep")])),
    )
    joined = await session.join()
    ws = socket()
    attached = asyncio.create_task(session.attach_handoff(ws, joined.handoff_id))
    try:
        await run_until(fake_clock, lambda: bool(ws.sent_json()))
        ws.feed_bytes(b"private audio")
        if reason == "company_hangup":
            await session.hangup(reason)
        else:
            await session.return_to_agent(joined.handoff_id)
            await run_until(fake_clock, lambda: session.ended)
        await asyncio.wait_for(attached, timeout=2)
        await session.wait_ended()
        assert_handoff_closed(rig, session, ws, reason)
        assert len(rig.gate.round_calls) == (1 if reason == "company_hangup" else 2)
    finally:
        attached.cancel()
        await asyncio.gather(attached, return_exceptions=True)
        await session.hangup("test cleanup")


async def test_company_hangup_after_announcement_does_not_wait_for_browser(mode_env, engine_config, fake_clock):
    company = ScriptedCompany(
        [line("The account holder must verify.", role="rep")],
        queue_reactions(When("speech", "account holder", [
            line("We cannot continue.", role="rep"), line("Goodbye", kind="hangup"),
        ])),
    )
    rig = make_rig(settings=build_settings(mode_env("text-sim"), engine_config),
                   clock=fake_clock, company=company, allowed_fields={},
                   script=[("must verify", spec_handoff())])
    session = await rig.start()
    try:
        await run_until(fake_clock, lambda: session.ended, max_ms=2000, what="hangup before browser joins")
        await session.wait_ended()
        assert session.status().phase == "ended"
        assert session.status().handoff is None
        assert rig.ended_calls == [rig.spec.call_id]
        assert len(rig.gate.round_calls) == 1
        assert rig.events("handoff_ended")[0].action == {"dropped_sonic_outputs": 0}
        assert company.handoffs() == []
        assert all(e.transcript != "We cannot continue." for e in rig.events())
    finally:
        await session.hangup("test cleanup")
