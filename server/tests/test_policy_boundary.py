"""AC-49：阈值边界 0.85 记 auto_high_conf、0.84 记 low_stakes_top，两者都取概率最高项（G 线；tech §6.6、§7.1）。

第 3 步可见测试。按 below_threshold=top_choice 的 5 处参数化：DP-A1、DP-P1、DP-P2（Policy.evaluate_point）
与 DP-C1 的 menu、confidence 两步（Policy.evaluate_round）；比较含等号。既经 DecisionGate 驱动，
也直接调 Policy.evaluate_point。另测「结果跟着 Jev 走」：程序不自己挑选项。
"""

from pathlib import Path

import pytest

from fakes import round_answers
from g_support import (
    COMPANY_SKILL,
    NEUTRAL_TEXT,
    TASK_ID,
    PointChooser,
    build_kit,
    choice_answer,
    line,
    menu_turn,
    loaded_policy,
    prefer,
    profile,
    require_skills_dir,
    speech_turn,
    tool_candidates,
)
from waitless.decision.registry import load_registry
from waitless.models import (
    ASSISTANT_FIXED_ACTIONS,
    NONE_OF_THE_ABOVE,
    USER_ACTION_TOOLS,
    Candidate,
    DecisionContext,
    DecisionRequest,
)
from waitless.skills.loader import load_skills

BOUNDARY = [(0.85, "auto_high_conf"), (0.84, "low_stakes_top")]
USER_TEXT = "Please start electricity at my new apartment on November 1."

SINGLE = {
    "assistant_next_action": dict(
        candidates=tool_candidates(),
        top="prepare_task",
        runner_up="reply_only",
        exclude=(NONE_OF_THE_ABOVE, *USER_ACTION_TOOLS),
    ),
    "company_skill": dict(
        candidates=[Candidate(id=COMPANY_SKILL, label="Lakeshore Energy", description="Electric service in Chicago")],
        top=COMPANY_SKILL,
        runner_up=NONE_OF_THE_ABOVE,
        exclude=(NONE_OF_THE_ABOVE,),
    ),
    "task_intent": dict(
        candidates=[Candidate(id="start_service", label="Start service", description="Start electric service")],
        top="start_service",
        runner_up=NONE_OF_THE_ABOVE,
        exclude=(NONE_OF_THE_ABOVE,),
    ),
}


def _kit(skills_dir, test_profile, fake_jev, fake_clock):
    return build_kit(skills_dir, jev=fake_jev, clock=fake_clock, prof=profile(test_profile))


@pytest.mark.parametrize("point", sorted(SINGLE))
@pytest.mark.parametrize("confidence, reason", BOUNDARY)
async def test_ac49_single_point_boundary_through_the_gate(
    skills_dir: Path, test_profile: dict, fake_jev, fake_clock, point: str, confidence: float, reason: str
) -> None:
    kit = _kit(skills_dir, test_profile, fake_jev, fake_clock)
    case = SINGLE[point]
    chooser = PointChooser(prefer(case["top"], exclude=case["exclude"]), confidence=confidence)
    fake_jev.push(chooser)
    result = await kit.decide(point, candidates=case["candidates"], transcript=[line("user", USER_TEXT)])
    assert len(fake_jev.calls) == 1
    assert result.reason_code == reason
    assert result.intent == chooser.only_choice
    assert result.turn_seq is None  # 缺口 D-G-2：只有 call_round 的结果带 turn_seq
    assert result.action == "wait"  # 歧义定稿 A-02：非通话决策点 action 固定 wait
    assert kit.trace.has_decision(result.decision_id)
    # 缺口 A-G-6（定稿 gate_appends）：调用方只给领域候选，gate 自己补固定动作与 none_of_the_above
    criteria = next(iter(chooser.criteria.values()))
    if point == "assistant_next_action":
        assert set(ASSISTANT_FIXED_ACTIONS) <= set(criteria)
    else:
        assert NONE_OF_THE_ABOVE in criteria


@pytest.mark.parametrize("point", sorted(SINGLE))
@pytest.mark.parametrize("confidence, reason", BOUNDARY)
def test_ac49_evaluate_point_boundary(skills_dir: Path, point: str, confidence: float, reason: str) -> None:
    require_skills_dir(skills_dir)
    registry = load_registry(load_skills(skills_dir)).validate()
    policy = loaded_policy(skills_dir)
    case = SINGLE[point]
    [qid] = [q for q, spec in registry.point(point).questions.items() if spec.type == "choice"]
    request = _bare_request(point, case["candidates"])
    answers = {qid: choice_answer(case["top"], confidence, [case["top"], case["runner_up"]])}
    outcome = policy.evaluate_point(point, request, answers)
    assert outcome.reason_code == reason
    assert outcome.intent == case["top"]


def _bare_request(point: str, candidates) -> DecisionRequest:
    return DecisionRequest(
        decision_id="req-boundary",
        point=point,
        call_id=None,
        task_id=TASK_ID,
        turn=None,
        candidates=list(candidates),
        context=DecisionContext(
            task_card=None, allowed_fields={}, standing_rules=[], prior_choices=[], interrupts_used=0, phase="idle"
        ),
        transcript=[line("user", USER_TEXT)],
    )


@pytest.mark.parametrize("confidence, reason", BOUNDARY)
async def test_ac49_menu_step_boundary(
    skills_dir: Path, test_profile: dict, fake_jev, fake_clock, confidence: float, reason: str
) -> None:
    kit = _kit(skills_dir, test_profile, fake_jev, fake_clock)
    fake_jev.push(round_answers("2", menu_confidence=confidence, call_phase="ivr_menu"))
    result = await kit.round(menu_turn(), phase="ivr")
    assert result.reason_code == reason
    assert result.intent == "2"
    assert result.action == "press" and result.digits == "2", "the press follows Jev's pick, not the program's"


@pytest.mark.parametrize("confidence, reason", BOUNDARY)
async def test_ac49_conversation_confidence_step_boundary(
    skills_dir: Path, test_profile: dict, fake_jev, fake_clock, confidence: float, reason: str
) -> None:
    kit = _kit(skills_dir, test_profile, fake_jev, fake_clock)
    fake_jev.push(round_answers("open_answer", menu_confidence=confidence, call_phase="ai_agent"))
    result = await kit.round(speech_turn(NEUTRAL_TEXT), phase="ai_agent")
    assert result.reason_code == reason
    assert result.intent == "open_answer"
    assert result.action == "release"


async def test_ac49_conversation_follows_jev_pick(skills_dir: Path, test_profile: dict, fake_jev, fake_clock) -> None:
    kit = _kit(skills_dir, test_profile, fake_jev, fake_clock)
    fake_jev.push(round_answers("wait", menu_confidence=0.9, call_phase="hold"))
    result = await kit.round(speech_turn("Please hold for a moment while I pull that up."), phase="hold")
    assert result.intent == "wait" and result.action == "wait"
    assert result.reason_code == "auto_high_conf"


async def test_ac49_menu_none_of_the_above_goes_to_the_safe_default(
    skills_dir: Path, test_profile: dict, fake_jev, fake_clock
) -> None:
    kit = _kit(skills_dir, test_profile, fake_jev, fake_clock)
    fake_jev.push(round_answers(NONE_OF_THE_ABOVE, menu_confidence=0.9, call_phase="ivr_menu"))
    result = await kit.round(menu_turn(), phase="ivr")
    assert "menu_safe_default" in {result.reason_code, result.intent}
    assert not (result.action == "press" and result.digits in {"1", "2"}), "never press an option Jev rejected"
