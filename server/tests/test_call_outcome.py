"""AC-37：挂断后，只有 Nova 2 Lite 抽出的确认号匹配公司 skill 的 confirmation_pattern，且 Jev call_outcome 以 ≥0.85 选
confirmed，结果才是 confirmed；否则是 needs_follow_up。

机制（tech §7.1）：ResultService.finalize(call_id)：L4 extract_result_items() 拿确认号 → decide("call_outcome")；
阈值由 G 线策略执行（gate 返回策略之后的 intent），A 侧核对确认号是否匹配公司 skill（ivr-map.yaml）的
confirmation_pattern，并写 calls 行。定稿 A-24：finalize 返回 models.CallResult。
ScriptedGate 的 Pick 同时给出 Jev 原始选择与策略结果：例如 Jev 0.80 选 confirmed，策略降为 needs_follow_up。
"""

import pytest

from waitless import models as m

from a_support import AEnv, Pick, RecordingLlm, ScriptedGate, company_turn, utterance

LINES = [("company", "Thanks for calling Lakeshore Energy."), ("waitless", "I'm calling to start service."),
         ("company", "You're all set for November 1. Your confirmation number is LE-48291.")]


@pytest.fixture
def env(tmp_path):
    e = AEnv(tmp_path)
    yield e
    e.close()


async def _finalize(env, call_id, outcome_pick, numbers):
    task_id = env.add_task(f"task-{call_id}", status="in_call")
    env.add_call(task_id, call_id, LINES)
    gate = ScriptedGate({"call_outcome": outcome_pick, "result_item_kind": "follow_up"})
    llm = RecordingLlm(result_items=m.ResultItems(confirmation_numbers=numbers, statements=[]),
                       summary="Service starts November 1.")
    out = await env.result_service(gate=gate, llm=llm).finalize(call_id)
    return out, gate, llm, task_id


@pytest.mark.parametrize(
    ("pick", "numbers", "expected", "number"),
    [
        (Pick("confirmed", confidence=0.95), ["LE-48291"], "confirmed", "LE-48291"),
        (Pick("confirmed", confidence=0.95), ["48291"], "needs_follow_up", None),  # 不符合 ^LE-\d{5}$
        (Pick("confirmed", confidence=0.95), [], "needs_follow_up", None),  # 没抽出确认号
        (Pick("needs_follow_up", choice="confirmed", confidence=0.80), ["LE-48291"], "needs_follow_up", None),  # 低于 0.85
    ],
    ids=["match-and-confident", "pattern-mismatch", "no-number", "below-threshold"],
)
async def test_ac37_confirmed_needs_matching_number_and_confident_outcome(env, pick, numbers, expected, number):
    out, gate, llm, task_id = await _finalize(env, "call-37", pick, numbers)
    assert isinstance(out, m.CallResult)
    assert out.call_id == "call-37"
    assert out.outcome == expected
    assert out.confirmation_number == number
    [row] = env.store.list_calls(task_id)
    assert row["outcome"] == expected
    assert row["confirmation_number"] == number
    assert len(gate.requests_for("call_outcome")) == 1
    assert gate.requests_for("call_outcome")[0].call_id == "call-37"


async def test_ac37_l4_reads_this_calls_transcript_from_trace(env):
    out, gate, llm, task_id = await _finalize(env, "call-37t", Pick("confirmed"), ["LE-48291"])
    [call] = llm.calls_for("extract_result_items")
    texts = [line.text for line in call["transcript"]]
    assert texts == [text for _, text in LINES]
    speakers = [line.speaker for line in call["transcript"]]
    assert speakers == [speaker for speaker, _ in LINES]


TASK_STATUS_AFTER = {"confirmed": "completed", "needs_follow_up": "needs_follow_up", "failed": "failed"}


@pytest.mark.parametrize(("pick", "numbers", "expected"), [
    (Pick("confirmed", confidence=0.95), ["LE-48291"], "confirmed"),
    (Pick("needs_follow_up", choice="confirmed", confidence=0.7), ["LE-48291"], "needs_follow_up"),
], ids=["confirmed", "follow-up"])
async def test_ac37_on_ended_runs_the_result_flow(env, pick, numbers, expected):
    """r3 缺口 A-G-8（定稿 results_kwarg）：通话结束时 CallDeps.on_ended → CallManager.on_ended → results.finalize，
    calls 行写 outcome 与确认号，tasks.status 随结果变化。"""
    task_id, version = await env.ready_task()
    gate = ScriptedGate({"call_outcome": pick, "result_item_kind": "follow_up"}, trace=env.trace)
    llm = RecordingLlm(result_items=m.ResultItems(confirmation_numbers=numbers, statements=[]))
    calls = env.call_manager(gate=gate, llm=llm, results=env.result_service(gate=gate, llm=llm))
    started = await calls.start(task_id, True, version)
    env.clock.advance_sync(3_000)
    env.trace.event(company_turn(task_id, started.call_id, "You're all set. Confirmation LE-48291.",
                                 t_ms=env.clock.monotonic_ms()))
    env.trace.event(utterance(task_id, started.call_id, "Thank you, goodbye.", t_ms=env.clock.monotonic_ms() + 500))
    await calls.on_ended(started.call_id)  # CallDeps.on_ended 指向它（app 组装时接好）
    [row] = env.store.list_calls(task_id)
    assert row["call_id"] == started.call_id
    assert row["outcome"] == expected
    assert row["confirmation_number"] == ("LE-48291" if expected == "confirmed" else None)
    assert env.store.get_task(task_id)["status"] == TASK_STATUS_AFTER[expected]
    [req] = gate.requests_for("call_outcome")
    assert req.call_id == started.call_id
