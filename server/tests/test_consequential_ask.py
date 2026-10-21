"""AC-55：任务卡上没有、含金额的选项，即使 Jev 置信度 0.95 也不自动答，生成待决选项（G 线；tech §6.6 第 4 步）。

第 3 步可见测试。PendingChoice 由动作执行器（D/E）收到 ask_user 后调 PendingRegistry.create() 建立；
G 侧断言策略结果是 ask_user。
"""

from pathlib import Path

from fakes import round_answers
from g_support import FEE_TEXT, build_kit, profile, speech_turn


async def test_ac55_money_choice_not_on_the_card_asks_the_user(
    skills_dir: Path, test_profile: dict, fake_jev, fake_clock
) -> None:
    kit = build_kit(skills_dir, jev=fake_jev, clock=fake_clock, prof=profile(test_profile))
    fake_jev.push(round_answers("open_answer", menu_confidence=0.95, call_phase="live_rep", needs_user=0.1, stakes=0))
    result = await kit.round(speech_turn(FEE_TEXT), interrupts_used=0)
    assert result.intent == "ask_user"
    assert result.action == "ask_user"


async def test_ac55_contract_question_asks_even_when_jev_would_end(
    skills_dir: Path, test_profile: dict, fake_jev, fake_clock
) -> None:
    kit = build_kit(skills_dir, jev=fake_jev, clock=fake_clock, prof=profile(test_profile))
    fake_jev.push(round_answers("confirm_and_end", menu_confidence=0.95, call_phase="live_rep", needs_user=0.3, stakes=3))
    result = await kit.round(
        speech_turn("To finish, we need you to agree to a twelve-month service contract. Is that okay?"),
        interrupts_used=0,
    )
    assert result.intent == "ask_user" and result.action == "ask_user"
