"""A company hangup must interrupt the pending-user wait."""
import pytest

from e_support import (
    ScriptedCompany, build_settings, line, make_rig, run_until, spec_ask_user,
)


@pytest.mark.parametrize("signal", ["hangup_line", "ended_flag"])
async def test_company_hangup_during_pending_announcement_ends_without_timeout(
    signal, mode_env, engine_config, fake_clock,
):
    def reactions(kind, value, company):
        if kind == "speech" and "One moment" in value:
            if signal == "ended_flag":
                company._ended = True
                return []
            return [line("We cannot wait. Goodbye.", kind="hangup")]
        return []

    company = ScriptedCompany([line("Choose an offer.")], reactions)
    rig = make_rig(
        settings=build_settings(mode_env("text-sim"), engine_config),
        clock=fake_clock, company=company, allowed_fields={},
        script=[("Choose an offer", spec_ask_user())], timeout_sec=30,
    )
    session = await rig.start()
    try:
        await run_until(fake_clock, lambda: session.ended, max_ms=2000,
                        what="company hangup before pending timeout")
        await session.wait_ended()
        assert rig.ended_calls == [rig.spec.call_id]
        assert len(rig.gate.round_calls) == 1
        assert rig.events("pending_expired") == []
        assert len(rig.events("remote_hangup")) == 1
        assert rig.events("call_ended")[0].action == {"reason": "company_hangup"}
        assert not any("not available" in text for text in company.heard())
    finally:
        await session.hangup("test cleanup")
