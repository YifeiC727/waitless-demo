"""AC-25：同一任务打过两通电话后，get_case_result.calls 有两条 task_id 相同的记录，history 含两通的事件。

机制（tech §7.1）：calls 表按 task_id 一对多；ResultService.case_result(task_id) 合并多通的 history。
每通挂断后先走 ResultService.finalize(call_id)（定稿 A-24：返回一通的 CallResult），再取任务级结果。
「history 含两通的事件」用不依赖文案的方式断言：第二通结束后的 history 包含第一通结束后的全部条目，并且更多。
r3 缺口 A-G-3（定稿 skills_and_clock）：ResultService 收 clock，ended_at = finalize 时的 clock.now_iso()，
duration_s 按 calls.started_at 与 clock 算——这里断言它们等于 FakeClock 推进的量。
"""

import json
from datetime import timedelta

import pytest

from waitless import models as m

from a_support import AEnv, Pick, RecordingLlm, ScriptedGate

FIRST = [("company", "Thank you for calling Lakeshore Energy."), ("waitless", "I am calling to start service."),
         ("company", "Our systems are down, please call back later.")]
SECOND = [("company", "Thank you for calling Lakeshore Energy."), ("waitless", "I am calling to start service."),
          ("company", "Service will start on November 1. Your confirmation number is LE-48291.")]


@pytest.fixture
def env(tmp_path):
    e = AEnv(tmp_path)
    yield e
    e.close()


def _keys(items) -> set[str]:
    return {json.dumps(i.model_dump(mode="json"), sort_keys=True) for i in items}


async def test_ac25_two_calls_on_one_task_both_in_case_result(env):
    task_id = env.add_task("task-25", status="in_call")
    gate = ScriptedGate({"call_outcome": [Pick("needs_follow_up", confidence=0.9), Pick("confirmed", confidence=0.95)],
                         "result_item_kind": "follow_up"})
    llm = RecordingLlm(result_items=lambda lines: m.ResultItems(
        confirmation_numbers=["LE-48291"] if any("LE-48291" in line.text for line in lines) else [],
        statements=[]))
    results = env.result_service(gate=gate, llm=llm)

    started_a = env.clock.now()
    env.add_call(task_id, "call-25-a", FIRST)  # 3 句，每句前、挂断前各推进 4 s：共 16 s
    await results.finalize("call-25-a")
    after_first = results.case_result(task_id)
    assert [c.call_id for c in after_first.calls] == ["call-25-a"]
    assert after_first.history, "history must list the first call's events"

    env.clock.advance_sync(60_000)
    env.add_call(task_id, "call-25-b", SECOND)
    await results.finalize("call-25-b")
    after_second = results.case_result(task_id)

    assert sorted(c.call_id for c in after_second.calls) == ["call-25-a", "call-25-b"]
    rows = env.store.list_calls(task_id)
    assert sorted(r["call_id"] for r in rows) == ["call-25-a", "call-25-b"]
    assert {r["task_id"] for r in rows} == {task_id}
    assert _keys(after_first.history) <= _keys(after_second.history)
    assert len(after_second.history) > len(after_first.history)
    by_id = {c.call_id: c for c in after_second.calls}
    assert by_id["call-25-a"].outcome == "needs_follow_up"
    assert by_id["call-25-b"].outcome == "confirmed"
    first = by_id["call-25-a"]
    assert first.started_at == started_a
    assert first.ended_at == started_a + timedelta(seconds=16)
    assert first.duration_s == pytest.approx(16.0)
    assert by_id["call-25-b"].duration_s == pytest.approx(16.0)
