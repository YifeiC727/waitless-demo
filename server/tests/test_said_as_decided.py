"""AC-72（G 侧）：DP-C2 said_as_decided 的题目与策略（G 线；tech §6.6、§7.1；第 3 步可见测试）。

D 线负责注入后取下一段 ASSISTANT FINAL、按结果再注入或走安全默认并记 say_mismatch（D 线测）。
G 侧按歧义定稿 A-02（wait_except_c2）：
  通过（≥0.7，含等号）       → action=wait，intent=said_ok
  第一次未过（attempt=0）    → action=say，say_text=原注入文字，intent=reinject
  第二次仍未过（attempt=1）  → 按 policy.yaml 的 said_as_decided.safe_default 出动作，reason_code=say_mismatch
DecisionRequest.attempt 待骨架 r2 加入。decision_summary 是本次注入的话术文字（§7.1：取本次 say 模板的文字）。
"""

from pathlib import Path
from typing import get_args

from g_support import CALL_ID, PointChooser, build_kit, line, merged_policy_raw, point_questions, prefer, profile
from waitless.models import Action

INJECTED = "We'd like to pay the security deposit, please."
UTTERANCE = "Sure, we'll go ahead with the security deposit."
ACTIONS = set(get_args(Action))


async def _said(kit, p: float, *, attempt: int | None = None, utterance: str = UTTERANCE):
    kit.jev.push(PointChooser(prefer(), noul=p))
    return await kit.decide(
        "said_as_decided",
        call_id=CALL_ID,
        phase="live_rep",
        with_card=True,
        transcript=[line("company", "Would you like a deposit or autopay?"), line("waitless", utterance)],
        template_vars={"decision_summary": INJECTED},
        attempt=attempt,
    )


async def test_ac72_said_question_comes_from_the_skill(skills_dir: Path, test_profile: dict, fake_jev, fake_clock) -> None:
    kit = build_kit(skills_dir, jev=fake_jev, clock=fake_clock, prof=profile(test_profile))
    await _said(kit, 0.9)
    [call] = fake_jev.calls
    [(qid, question)] = call.questions.items()
    assert question.type == "noul"
    spec = point_questions(skills_dir, "said_as_decided")
    assert question.instructions == spec[qid]["instructions"].replace("{decision_summary}", INJECTED)
    assert UTTERANCE not in question.instructions, "what Sonic said is transcript, not instructions"
    assert call.state.transcript.lines[-1].speaker == "waitless"
    assert call.state.transcript.lines[-1].text == UTTERANCE
    [event] = kit.trace.decisions()
    assert event.decision_point == "said_as_decided"


async def test_ac72_pass_at_and_above_0_7(skills_dir: Path, test_profile: dict, fake_jev, fake_clock) -> None:
    kit = build_kit(skills_dir, jev=fake_jev, clock=fake_clock, prof=profile(test_profile))
    for p in (0.7, 0.95):
        result = await _said(kit, p)
        assert result.intent == "said_ok", p
        assert result.action == "wait", p


async def test_ac72_first_miss_reinjects_the_same_text(skills_dir: Path, test_profile: dict, fake_jev, fake_clock) -> None:
    kit = build_kit(skills_dir, jev=fake_jev, clock=fake_clock, prof=profile(test_profile))
    result = await _said(kit, 0.69)  # attempt 缺省为 0
    assert result.intent == "reinject"
    assert result.action == "say"
    assert result.say_text == INJECTED


async def test_ac72_second_miss_goes_to_the_safe_default(
    skills_dir: Path, test_profile: dict, fake_jev, fake_clock
) -> None:
    kit = build_kit(skills_dir, jev=fake_jev, clock=fake_clock, prof=profile(test_profile))
    result = await _said(kit, 0.2, attempt=1)
    assert result.reason_code == "say_mismatch"
    assert result.intent == "safe_default"
    assert result.action != "say"
    safe_default = merged_policy_raw(skills_dir)["decision_points"]["said_as_decided"]["safe_default"]
    if safe_default in ACTIONS:
        assert result.action == safe_default


def test_ac72_policy_values(skills_dir: Path) -> None:
    said = merged_policy_raw(skills_dir)["decision_points"]["said_as_decided"]
    assert said["min_probability"] == 0.7
    assert said["retries"] == 1
    assert said["on_fail"] == "safe_default"
    assert "safe_default" in said, "A-02: policy.yaml names the action for the second miss"
