"""Call debug data must come from this call's decision history."""
import pytest

from e_support import (
    ScriptedCompany, build_settings, line, make_rig, run_until, spec_end, spec_wait,
)


@pytest.mark.parametrize("other_call_id", ["another-call", None])
async def test_debug_last_decision_ignores_other_call_and_assistant_decisions(
    other_call_id, mode_env, engine_config, fake_clock,
):
    rig = make_rig(
        settings=build_settings(mode_env("text-sim"), engine_config),
        clock=fake_clock, company=ScriptedCompany([line("Completed.")]),
        allowed_fields={}, script=[("Completed", spec_end())],
    )
    session = await rig.start()
    await run_until(fake_clock, lambda: session.ended)
    await session.wait_ended()
    own = session.status(debug=True).debug.last_decision
    other = rig.gate.record(
        spec_wait(), task_id=rig.spec.task_id, call_id=other_call_id,
        point="assistant_next_action" if other_call_id is None else "call_round",
    )
    assert other.decision_id != own.decision_id
    assert session.status(debug=True).debug.last_decision == own
