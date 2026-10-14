from datetime import datetime

import pytest

from a_support import AEnv, RecordingLlm, ScriptedGate
from waitless import models as m


@pytest.mark.parametrize("finalize_delay_ms", [0, 5000])
async def test_call_duration_excludes_post_hangup_processing(tmp_path, finalize_delay_ms):
    env = AEnv(tmp_path)
    try:
        task_id = env.add_task("task", status="in_call")
        env.add_call(task_id, "call", [("company", "We will follow up.")])
        end = env.clock.now_iso()
        started = env.store.list_calls(task_id)[0]["started_at"]
        duration = (datetime.fromisoformat(end) - datetime.fromisoformat(started)).total_seconds()
        env.clock.advance_sync(finalize_delay_ms)

        def extract(transcript):
            env.clock.advance_sync(9000)
            return m.ResultItems(confirmation_numbers=[], statements=[])

        service = env.result_service(gate=ScriptedGate({"call_outcome": "needs_follow_up"}),
                                     llm=RecordingLlm(result_items=extract))
        await service.finalize("call")
        [row] = env.store.list_calls(task_id)
        assert row["ended_at"] == end
        assert row["duration_s"] == duration
        history_end = next(h for h in service.case_result(task_id).history if h.title == "Call Ended")
        assert history_end.t == datetime.fromisoformat(end)
        env.clock.advance_sync(2000)
        await service.finalize("call")
        assert env.store.list_calls(task_id)[0]["ended_at"] == end
    finally:
        env.close()
