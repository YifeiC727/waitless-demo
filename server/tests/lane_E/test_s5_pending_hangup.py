"""A company hangup must interrupt the pending-user wait."""
import pytest

from e_support import (
    ScriptedCompany, build_settings, line, make_rig, run_until, spec_ask_user,
    spec_end, spec_wait,
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


@pytest.mark.parametrize("signal", ["hangup_line", "ended_flag", "unflagged_hangup"])
@pytest.mark.parametrize("confirmation_action", ["wait", "end"])
async def test_pending_hangup_drains_confirmation_batch_before_ending(
    signal, confirmation_action, mode_env, engine_config, fake_clock,
):
    confirmation = "Your confirmation is REF-73429."
    goodbye = "Company goodbye."
    pending_waiter = None
    heard_at_hangup = []

    def reactions(kind, value, company):
        if kind == "speech" and "One moment" in value:
            heard_at_hangup.extend(company.heard())
            if signal == "ended_flag":
                company._ended = True
                return [line(confirmation, confirmation_number="REF-73429")]
            return [line(confirmation, confirmation_number="REF-73429"),
                    line(goodbye, kind="hangup")]
        return []

    class UnflaggedCompany(ScriptedCompany):
        def _emit(self, lines):
            out = super()._emit(lines)
            if signal == "unflagged_hangup":
                self._ended = False
            return out

    company = UnflaggedCompany([line("Choose an offer.")], reactions)

    def decisions(turn, ctx):
        nonlocal pending_waiter
        if "Choose an offer" in turn.text:
            return spec_ask_user()
        pending_waiter = session._choice_answer
        assert ctx.context.phase == "live_rep"
        return spec_end() if confirmation_action == "end" else spec_wait()

    rig = make_rig(
        settings=build_settings(mode_env("text-sim"), engine_config),
        clock=fake_clock, company=company, allowed_fields={},
        script=decisions, timeout_sec=30,
    )
    session = rig.create()
    # Capture the waiting future during DP-C2, after the announcement is sent.
    waiters = []
    original_decide = rig.gate.decide

    async def decide(point, request):
        waiters.append(session._choice_answer)
        return await original_decide(point, request)

    rig.gate.decide = decide
    await session.start()
    try:
        await run_until(fake_clock, lambda: session.ended, max_ms=2000,
                        what="confirmation batch drained before pending deadline")
        await session.wait_ended()
        assert [turn.text for turn in rig.gate.turns()] == ["Choose an offer.", confirmation]
        assert rig.texts("company_turn") == ["Choose an offer.", confirmation]
        assert [item.text for item in session.lines()] == (
            ["Choose an offer.", confirmation, goodbye]
            if signal != "ended_flag" and confirmation_action == "wait"
            else ["Choose an offer.", confirmation]
        )
        assert rig.events("pending_expired") == []
        assert not any(event.action.get("template") in ("pending.answer", "pending.safe_default")
                       for event in rig.events("injection_sent"))
        assert session._choice_answer is None and pending_waiter is None
        assert waiters[0].cancelled()
        assert not any(seconds >= 2 for seconds in fake_clock.sleep_calls)
        if confirmation_action == "wait":
            assert company.heard() == heard_at_hangup
            assert len(rig.events("remote_hangup")) == 1
            assert rig.events("hangup") == []
        else:
            assert rig.events("remote_hangup") == []
            assert len(rig.events("hangup")) == 1
        assert rig.ended_calls == [rig.spec.call_id]
        assert len(rig.events("call_ended")) == 1
    finally:
        await session.hangup("test cleanup")
