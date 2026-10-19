"""AC-54：坐席问押金意愿时，按任务卡上已确认的值注入话术，不生成待决选项（G 线；tech §6.6 第 4 步）。

第 3 步可见测试。say_text 用 say-templates.yaml 的 answer_precall 模板，填入任务卡所选选项的 label。
"""

from pathlib import Path

from fakes import round_answers
from g_support import build_kit, profile, speech_turn, task_card
from waitless.models import PrecallDecision


APPROVE_LABEL = "Yes, I am willing to pay a refundable deposit"
ASK_ME_LABEL = "No, I will decide on the call"
DEPOSIT_TEXT = "Are you willing to pay a refundable deposit?"


def _deposit_consent_card(selected):
    card = task_card(precall=False)
    card.precall_decisions.append(PrecallDecision(
        question_id="deposit_consent",
        question=("A refundable deposit may be required. The account holder will confirm it on the call. "
                  "Should Waitless tell the company you are willing to pay it?"),
        options=[
            {"id": "approve", "label": APPROVE_LABEL,
             "detail": "You still confirm it yourself on the call"},
            {"id": "ask_me", "label": ASK_ME_LABEL,
             "detail": "Waitless does not agree to a deposit for you"},
        ],
        default=None, selected=selected, needs_user=False,
    ))
    return card


async def test_ac54_deposit_question_is_answered_from_the_task_card(
    skills_dir: Path, test_profile: dict, fake_jev, fake_clock
) -> None:
    kit = build_kit(skills_dir, jev=fake_jev, clock=fake_clock, prof=profile(test_profile),
                    card=_deposit_consent_card("approve"))
    fake_jev.push(round_answers("ask_user", menu_confidence=0.95, call_phase="live_rep", needs_user=0.6, stakes=3))
    result = await kit.round(speech_turn(DEPOSIT_TEXT))
    assert result.intent == "answer_precall:deposit_consent"
    assert result.action == "say"
    assert APPROVE_LABEL in result.say_text
    assert ASK_ME_LABEL not in result.say_text
    assert result.pending is None


async def test_ac54_open_answer_pick_is_overridden_by_the_card(
    skills_dir: Path, test_profile: dict, fake_jev, fake_clock
) -> None:
    kit = build_kit(skills_dir, jev=fake_jev, clock=fake_clock, prof=profile(test_profile),
                    card=_deposit_consent_card("ask_me"))
    fake_jev.push(round_answers("open_answer", menu_confidence=0.95, call_phase="live_rep", needs_user=0.1, stakes=3))
    result = await kit.round(speech_turn(DEPOSIT_TEXT))
    assert result.intent == "answer_precall:deposit_consent"
    assert result.action == "say" and ASK_ME_LABEL in result.say_text
