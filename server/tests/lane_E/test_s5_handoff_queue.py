"""Private handoff batches must stay private after the agent resumes."""
import pytest

from e_support import (
    ScriptedCompany, When, build_settings, line, make_rig, queue_reactions,
    run_until, spec_end, spec_handoff, spec_wait,
)


@pytest.mark.parametrize("let_worker_pause", [False, True])
async def test_handoff_keeps_agent_queue_and_excludes_private_holder_lines(
    let_worker_pause, mode_env, engine_config, fake_clock,
):
    queued = "Please tell the account holder to verify privately."
    private = "Private holder conversation."
    company = ScriptedCompany(
        [line("Account holder required.", role="rep")],
        queue_reactions(
            When("speech", "bring the account holder", [line(queued, role="rep")]),
            When("handoff", True, [line(private, role="rep")]),
            When("handoff", False, [line("Completed verification.", role="rep")]),
        ),
    )
    rig = make_rig(
        settings=build_settings(mode_env("text-sim"), engine_config),
        clock=fake_clock, company=company, allowed_fields={},
        script=[("Account holder required", spec_handoff()),
                (queued, spec_wait()),
                ("Completed verification", spec_end())],
    )
    session = await rig.start()
    try:
        await run_until(fake_clock, lambda: session.status().phase == "handoff_ready")
        if let_worker_pause:
            await fake_clock.advance(0)
        joined = await session.join()
        assert [turn.text for turn in rig.gate.turns()] == ["Account holder required."]
        assert private in [item.text for item in session.lines()]
        assert session.status().transcript_tail == []
        await session.return_to_agent(joined.handoff_id)
        await run_until(fake_clock, lambda: session.ended)
        await session.wait_ended()
        assert [turn.text for turn in rig.gate.turns()] == [
            "Account holder required.", queued, "Completed verification.",
        ]
        assert all(private not in event.model_dump_json() for event in rig.events())
        assert private not in [item.text for item in session.status().transcript_tail]
        assert [event.transcript for event in rig.events("company_turn")] == [
            "Account holder required.", queued, "Completed verification.",
        ]
        started, = rig.events("handoff_started")
        ended, = rig.events("handoff_ended")
        assert not [event for event in rig.events() if started.seq < event.seq < ended.seq]
        assert all(private not in [item.text for item in ctx.transcript]
                   for _, ctx in rig.gate.round_calls)
        assert rig.ended_calls == [rig.spec.call_id]
    finally:
        await session.hangup("test cleanup")
