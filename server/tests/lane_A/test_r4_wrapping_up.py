import asyncio
from datetime import datetime

import pytest

from a_support import AEnv, RecordingLlm, ScriptedGate, plain


class ControlledLlm(RecordingLlm):
    def __init__(self, stage, fail=False):
        super().__init__(summary="Final result summary.")
        self.stage, self.fail = stage, fail
        self.entered, self.release = asyncio.Event(), asyncio.Event()

    async def pause(self, stage):
        if stage == self.stage:
            self.entered.set()
            await self.release.wait()
            if self.fail:
                raise RuntimeError("result processing failed")

    async def extract_result_items(self, transcript):
        await self.pause("extract")
        return await super().extract_result_items(transcript)

    async def write_summary(self, *, result, transcript):
        await self.pause("summary")
        return await super().write_summary(result=result, transcript=transcript)


async def ended_call(env, llm):
    task_id, version = await env.ready_task()
    results = env.result_service(gate=ScriptedGate({"call_outcome": "needs_follow_up"}), llm=llm)
    manager = env.call_manager(results=results)
    started = await manager.start(task_id, True, version)
    session = manager.get_session(started.call_id)
    env.trace.event(plain("call_started", task_id, started.call_id, t_ms=env.clock.monotonic_ms()))
    env.clock.advance_sync(4000)
    await session.hangup("finished")
    env.trace.event(plain("call_ended", task_id, started.call_id, t_ms=env.clock.monotonic_ms()))
    return task_id, started.call_id, manager, results


@pytest.mark.parametrize("stage", ["extract", "summary"])
@pytest.mark.parametrize("debug", [False, True])
async def test_ended_is_delayed_until_final_result_is_written(tmp_path, stage, debug):
    env = AEnv(tmp_path)
    background = None
    llm = ControlledLlm(stage)
    try:
        task_id, call_id, manager, results = await ended_call(env, llm)
        end = env.clock.now_iso()
        background = asyncio.create_task(manager.on_ended(call_id))
        await asyncio.wait_for(llm.entered.wait(), timeout=2)
        out = manager.status(task_id, debug)
        assert out.ended is False and out.phase == "wrapping_up"
        assert (out.debug is not None) is debug
        assert results.case_result(task_id).outcome == "in_progress"
        assert env.store.list_calls(task_id)[0]["result_json"] is None
        env.clock.advance_sync(9000)
        llm.release.set()
        await background
        out = manager.status(task_id, debug)
        assert out.ended is True
        assert out.phase == manager.get_session(call_id).status(debug).phase
        result = results.case_result(task_id)
        assert result.outcome == "needs_follow_up" and result.summary == "Final result summary."
        [row] = env.store.list_calls(task_id)
        assert row["ended_at"] == end and row["duration_s"] == 4
        assert env.store.get_task(task_id)["status"] == "needs_follow_up"
    finally:
        llm.release.set()
        if background is not None:
            await asyncio.gather(background, return_exceptions=True)
        env.close()


@pytest.mark.parametrize("stage", ["extract", "summary"])
async def test_finalize_exception_ends_call_with_persisted_failed_result(tmp_path, stage):
    env = AEnv(tmp_path)
    background = None
    llm = ControlledLlm(stage, fail=True)
    try:
        task_id, call_id, manager, results = await ended_call(env, llm)
        end = env.clock.now_iso()
        # Even if the callback itself starts late, retain the hangup timestamp (K4).
        env.clock.advance_sync(2000)
        background = asyncio.create_task(manager.on_ended(call_id))
        await asyncio.wait_for(llm.entered.wait(), timeout=2)
        env.clock.advance_sync(9000)
        llm.release.set()
        with pytest.raises(RuntimeError, match="result processing failed"):
            await background
        assert manager.status(task_id).ended is True
        result = results.case_result(task_id)
        assert result.outcome == "failed" and result.summary != "The call is in progress."
        assert result.confirmation_number is None and result.verified == []
        [row] = env.store.list_calls(task_id)
        assert row["ended_at"] == end and row["duration_s"] == 4
        assert row["result_json"] and row["outcome"] == "failed"
        assert env.store.get_task(task_id)["status"] == "failed"
        # Repeated result reads retain the terminal failure rather than re-running L4/L5.
        await manager.on_ended(call_id)
        assert env.store.list_calls(task_id)[0]["ended_at"] == end
        assert next(h.t for h in result.history if h.title == "Call Ended") == datetime.fromisoformat(end)
    finally:
        llm.release.set()
        if background is not None:
            await asyncio.gather(background, return_exceptions=True)
        env.close()


@pytest.mark.parametrize("missing", ["ended_at", "result_json"])
def test_both_persistent_completion_markers_are_required(tmp_path, missing):
    env = AEnv(tmp_path)
    try:
        # Build a registered session without invoking external services or an event loop.
        from a_support import FakeSession
        from waitless.models import CallSpec, CallResult
        task_id = env.add_task("task")
        manager = env.call_manager()
        spec = CallSpec(call_id="call", task_id=task_id, mode=env.settings.mode,
                        company_name="Lakeshore Energy", to_number=env.store.get_task(task_id)["company_id"],
                        scenario_id="baseline", compiled=env.compile(env.task_service(gate=ScriptedGate()).get_card(task_id)))
        session = FakeSession(spec, manager.deps)
        session._ended = True
        manager._sessions[spec.call_id] = session
        env.store.create_call(call_id=spec.call_id, task_id=task_id, scenario_id=spec.scenario_id,
                              mode=spec.mode, started_at=env.clock.now_iso(), skill_commits=[])
        markers = {"ended_at": env.clock.now_iso(), "result_json": CallResult(
            call_id=spec.call_id, outcome="needs_follow_up", confirmation_number=None,
            verified=[], follow_ups=[], summary="Done").model_dump_json()}
        markers[missing] = None
        env.store.update_call(spec.call_id, **markers)
        assert manager.status(task_id).ended is False
        assert manager.status(task_id).phase == "wrapping_up"
    finally:
        env.close()
