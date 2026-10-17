"""AC-30：拨号前问题：只有 Jev precall_default 选 user_chose:<选项> 且置信度 ≥0.85 时，才预选用户自己说过的选项；否则保持默认。

机制（tech §7.1）：TaskService 对 pre-call-questions.yaml 的每一题问 DP-P3；阈值由 G 线策略（assisted-call policy.yaml）执行，
gate 返回策略之后的 DecisionResult.intent。A 侧要做的是：按 intent（不是 Jev 的原始最高项）设定 selected。
ScriptedGate 模拟两种结果：autopay 题 Jev 选 user_chose:autopay 0.93 → intent=user_chose:autopay；
bill_delivery 题 Jev 选 user_chose:paper 只有 0.80 → 策略降为 keep_default。
"""

import pytest

from a_support import (
    LAKESHORE_QUESTIONS,
    AEnv,
    Pick,
    ScriptedGate,
    precall_by_option,
)

REQUEST = ("Start electricity with Lakeshore Energy at 1200 W Example St, Apt 4B on November 1. "
           "Put me on autopay, and maybe paper bills.")


@pytest.fixture
def env(tmp_path):
    e = AEnv(tmp_path)
    yield e
    e.close()


async def test_ac30_only_high_confidence_user_choice_changes_selected(env):
    gate = ScriptedGate({
        "company_skill": "company-lakeshore-energy",
        "task_intent": "start_service",
        "precall_default": precall_by_option({
            "autopay": Pick("user_chose:autopay", confidence=0.93),
            "paper": Pick("keep_default", choice="user_chose:paper", confidence=0.80, policy_rule="below_threshold"),
        }),
    })
    out = await env.task_service(gate=gate).prepare(REQUEST, None)
    decisions = {d.question_id: d for d in out.task_card.precall_decisions}

    deposit = decisions["deposit_or_autopay"]
    assert deposit.selected == "autopay"
    assert deposit.default is None
    assert deposit.needs_user is False

    assert "bill_delivery" not in decisions, "a below-threshold user choice keeps the question off the card (D38, v5.15)"
    assert set(decisions) == {"deposit_or_autopay"}

    # 每一题各问一次 DP-P3
    assert len(gate.requests_for("precall_default")) == len(LAKESHORE_QUESTIONS)
