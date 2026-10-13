"""AC-38：对每条抽出的结果陈述，只有 Jev result_item_kind 以 ≥0.85 判为 verified，才进 verified 列表；低于阈值的进 follow_ups。

A + G：A 侧（本文件）是 finalize(call_id) 先调 extract_result_items()（L4）拿到 statements，再逐条调
decide("result_item_kind", …)，按返回的 DecisionResult.intent 写进 CallResult.verified 或 follow_ups（drop 两边都不放）；
verified 每项带 evidence{call_id, turn_seq}。G 侧（Policy.evaluate_point 按 0.85 判定，低于阈值返回 follow_up）由 G 线测试覆盖。
ScriptedGate 模拟策略之后的结果：「deposit refund」Jev 0.80 选 verified，策略降为 follow_up。
"""

import pytest

from waitless import models as m

from a_support import AEnv, Pick, RecordingLlm, ScriptedGate, request_texts

LINES = [
    ("company", "Your service will start on November 1."),
    ("company", "We will mail your deposit refund within 30 days."),
    ("company", "Our office is closed on Sundays."),
    ("company", "Your confirmation number is LE-48291."),
]
STATEMENTS = {
    "start": "Service starts on November 1",
    "refund": "Deposit refund will be mailed within 30 days",
    "office": "The office is closed on Sundays",
}


def _kind(request):
    text = request_texts(request)
    if STATEMENTS["start"] in text:
        return Pick("verified", confidence=0.94)
    if STATEMENTS["refund"] in text:
        return Pick("follow_up", choice="verified", confidence=0.80, policy_rule="below_threshold")
    if STATEMENTS["office"] in text:
        return Pick("drop", confidence=0.92)
    raise AssertionError(f"result_item_kind request does not carry the statement text: {text!r}")


@pytest.fixture
def env(tmp_path):
    e = AEnv(tmp_path)
    yield e
    e.close()


def _contains(item, needle):
    return any(needle in (getattr(item, f, "") or "") for f in ("label", "value", "detail"))


async def test_ac38_statements_split_by_kind_decision(env):
    task_id = env.add_task("task-38", status="in_call")
    seqs = env.add_call(task_id, "call-38", LINES)
    items = m.ResultItems(confirmation_numbers=["LE-48291"], statements=[
        m.ResultStatement(text=STATEMENTS["start"], turn_seq=seqs[0]),
        m.ResultStatement(text=STATEMENTS["refund"], turn_seq=seqs[1]),
        m.ResultStatement(text=STATEMENTS["office"], turn_seq=seqs[2]),
    ])
    gate = ScriptedGate({"call_outcome": Pick("confirmed", confidence=0.95), "result_item_kind": _kind})
    out = await env.result_service(gate=gate, llm=RecordingLlm(result_items=items)).finalize("call-38")

    assert len(gate.requests_for("result_item_kind")) == 3, "one DP-E2 decision per statement"
    assert len(out.verified) == 1
    verified = out.verified[0]
    assert _contains(verified, STATEMENTS["start"])
    assert verified.evidence == m.Evidence(call_id="call-38", turn_seq=seqs[0])

    assert len(out.follow_ups) == 1
    assert _contains(out.follow_ups[0], STATEMENTS["refund"])

    everything = [*out.verified, *out.follow_ups]
    assert not any(_contains(i, STATEMENTS["office"]) for i in everything), "drop goes nowhere"
