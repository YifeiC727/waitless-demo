"""Private handoff batches must stay private after the agent resumes."""
import pytest

from e_support import (
    ScriptedCompany, When, build_settings, line, make_rig, queue_reactions,
    run_until, spec_end, spec_handoff,
)


@pytest.mark.parametrize("let_worker_pause", [False, True])
async def test_handoff_queued_lines_are_not_replayed_after_return(
    let_worker_pause, mode_env, engine_config, fake_clock,
):
    private = "Please tell the account holder to verify privately."
    company = ScriptedCompany(
        [line("Account holder required.", role="rep")],
        queue_reactions(
            When("speech", "bring the account holder", [line(private, role="rep")]),
            When("handoff", True, [line("Private holder conversation.", role="rep")]),
            When("handoff", False, [line("Completed verification.", role="rep")]),
        ),
    )
    rig = make_rig(
        settings=build_settings(mode_env("text-sim"), engine_config),
        clock=fake_clock, company=company, allowed_fields={},
        script=[("Account holder required", spec_handoff()),
                ("Completed verification", spec_end())],
    )
    session = await rig.start()
    try:
        await run_until(fake_clock, lambda: session.status().phase == "handoff_ready")
        if let_worker_pause:
            await fake_clock.advance(0)
        joined = await session.join()
        await session.return_to_agent(joined.handoff_id)
        await run_until(fake_clock, lambda: session.ended)
        await session.wait_ended()
        assert [turn.text for turn in rig.gate.turns()] == [
            "Account holder required.", "Completed verification.",
        ]
        assert all(private not in event.model_dump_json() for event in rig.events())
        assert all("Private holder conversation" not in event.model_dump_json()
                   for event in rig.events())
        assert private not in [item.text for item in session.status().transcript_tail]
        assert rig.ended_calls == [rig.spec.call_id]
    finally:
        await session.hangup("test cleanup")
