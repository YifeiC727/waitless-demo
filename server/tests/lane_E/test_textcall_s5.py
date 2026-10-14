"""A result callback failure must not escape replay-error recovery."""

import asyncio

import pytest

from e_support import ScriptedCompany, build_settings, line, make_rig, run_until
from waitless.interfaces import ReplayMismatch


async def test_replay_error_survives_a_failing_result_callback_without_worker_error(
    mode_env, engine_config, fake_clock, monkeypatch, caplog,
):
    rig = make_rig(
        settings=build_settings(mode_env("text-sim"), engine_config),
        clock=fake_clock, company=ScriptedCompany([line("Please continue.")]), script=[], allowed_fields={},
    )
    mismatch = ReplayMismatch("jev", 7, "private original text")
    wrapper = RuntimeError("wrapped decision failed")
    wrapper.__cause__ = mismatch

    async def broken_round(*args):
        raise wrapper

    async def broken_finalize(call_id):
        raise RuntimeError("private result callback text")

    monkeypatch.setattr(rig.gate, "decide_round", broken_round)
    rig.deps.on_ended = broken_finalize
    session = await rig.start()
    try:
        await run_until(fake_clock, lambda: session.ended)
        with pytest.raises(ReplayMismatch) as error:
            await session.wait_ended()
        assert error.value is mismatch
        assert await session._worker is None
        assert [event.action for event in rig.events("error")] == [
            {"error_type": "ReplayMismatch", "channel": "jev", "seq": 7},
        ]
        assert "private" not in caplog.text
    finally:
        session._worker.cancel()
        await asyncio.gather(session._worker, return_exceptions=True)
