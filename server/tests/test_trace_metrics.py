"""AC-35：trace 指标：interrupts_used = pending_choice_created 与 handoff_ready 的次数之和；user_active_s = 任务卡确认和
待决选项作答的等待时长之和，不含转接期间。
另含 AC-96 的离线部分：菜单结束到按键音的毫秒数怎么从 trace 算（定稿 A-08：dtmf_sent.decision_id → decision 事件
action_json.turn_seq → 同一通里 action.seq 相同的 company_turn；company_turn 的 t_ms 是 CompanyTurn.t_end_ms）。
需密钥的 p50 ≤4000 ms 检查见 test_live_check.py。

机制（tech §7.1）：Trace.metrics(task_id) 按事件类型计数；用 user_action 的 shown_ms 与 acted_ms 计算等待时长；
Trace.menu_to_dtmf_ms(call_id) 给 check_live 用。
r3 缺口 A-G-9（定稿 server_timestamps）：user_action 只由 A 的 CallManager 写——start_confirmed 的 shown_ms 是任务卡以
ready 返回的时刻、acted_ms 是 start 被调用的时刻（clock.now_iso() 换算的毫秒级 Unix 时间）；见最后一条测试。
r3 缺口 A-G-10（定稿 menu_only_as_r2）：对话轮（kind=speech）里的按键（press_field）不计入 menu_to_dtmf_ms。
"""

import pytest

from a_support import (
    LAKESHORE_REQUEST,
    PROFILE_VALUES,
    AEnv,
    company_turn,
    consequential,
    decision_event,
    default_prepare_gate,
    plain,
    user_action,
)

TASK = "task-35"
CALL = "call-35"


def build_trace(trace) -> None:
    # 任务卡确认：卡片 1.0 s 显示，4.0 s 点 Start → 3.0 s
    trace.event(user_action(TASK, None, "start_confirmed", shown_ms=1_000, acted_ms=4_000, t_ms=4_000))
    trace.event(plain("call_started", TASK, CALL, t_ms=5_000))
    # 两层菜单：结束 → 按键 600 ms、2500 ms
    trace.event(company_turn(TASK, CALL, "For new service press 1.", kind="menu", turn_seq=0, t_ms=10_000))
    trace.event(decision_event(TASK, CALL, "m1", turn_seq=0, t_ms=10_300))
    trace.event(consequential("dtmf_sent", TASK, CALL, "m1", t_ms=10_600, action={"digits": "1"}))
    trace.event(company_turn(TASK, CALL, "To start service press 1. To stop service press 2.", kind="menu",
                             turn_seq=1, t_ms=14_000))
    trace.event(decision_event(TASK, CALL, "m2", turn_seq=1, t_ms=15_200))
    trace.event(consequential("dtmf_sent", TASK, CALL, "m2", t_ms=16_500, action={"digits": "1"}))
    # 对话轮里键入账户号（press_field）：不是菜单，不计入 menu_to_dtmf_ms
    trace.event(company_turn(TASK, CALL, "Please enter your account number, then press pound.", kind="speech",
                             turn_seq=4, t_ms=20_000))
    trace.event(decision_event(TASK, CALL, "k1", turn_seq=4, t_ms=20_300))
    trace.event(consequential("dtmf_sent", TASK, CALL, "k1", t_ms=21_000, action={"digits": "8800123456#"}))
    # 待决选项：显示 31.0 s，作答 33.5 s → 2.5 s；算一次打扰
    trace.event(company_turn(TASK, CALL, "Add $25 for November 1, or November 3 at no charge?", turn_seq=2, t_ms=30_000))
    trace.event(decision_event(TASK, CALL, "p1", turn_seq=2, t_ms=30_400))
    trace.event(consequential("pending_choice_created", TASK, CALL, "p1", t_ms=31_000))
    trace.event(user_action(TASK, CALL, "choice_answered", shown_ms=31_000, acted_ms=33_500, t_ms=33_500))
    # 转接：handoff_ready 算一次打扰；转接期间 60 s 的用户操作不计入 user_active_s
    trace.event(company_turn(TASK, CALL, "I need the account holder's date of birth.", turn_seq=3, t_ms=40_000))
    trace.event(decision_event(TASK, CALL, "h1", turn_seq=3, t_ms=40_200))
    trace.event(plain("handoff_announced", TASK, CALL, t_ms=40_500))
    trace.event(plain("handoff_ready", TASK, CALL, t_ms=41_000))
    trace.event(consequential("handoff_started", TASK, CALL, "h1", t_ms=41_500))
    trace.event(user_action(TASK, CALL, "handoff_joined", shown_ms=41_000, acted_ms=101_000, t_ms=101_000))
    trace.event(plain("handoff_ended", TASK, CALL, t_ms=101_500))
    trace.event(plain("call_ended", TASK, CALL, t_ms=120_000))


@pytest.fixture
def env(tmp_path):
    e = AEnv(tmp_path)
    build_trace(e.trace)
    yield e
    e.close()


def test_ac35_interrupts_used_counts_pending_choices_and_handoff_ready(env):
    assert env.trace.metrics(TASK).interrupts_used == 2


def test_ac35_user_active_seconds_only_card_and_choice(env):
    metrics = env.trace.metrics(TASK)
    assert metrics.task_id == TASK
    assert metrics.user_active_s == pytest.approx(5.5)


def test_ac96_menu_to_dtmf_ms_from_trace(env):
    assert env.trace.menu_to_dtmf_ms(CALL) == [600, 2_500]
    assert env.trace.metrics(TASK).menu_to_dtmf_ms == [600, 2_500]


async def test_ac35_start_writes_the_card_confirmation_wait(tmp_path):
    """经 prepare（ready）→ 用户 3.5 s 后点 Start → CallManager.start 写 user_action(start_confirmed)。"""
    env = AEnv(tmp_path)
    try:
        env.store.save_profile({k: PROFILE_VALUES[k] for k in ("name", "phone_number", "address")})
        env.store.save_profile({"account_number": PROFILE_VALUES["account_number"]}, company_id="lakeshore-energy")
        out = await env.task_service(gate=default_prepare_gate()).prepare(LAKESHORE_REQUEST, None)
        assert out.status == "ready" and out.missing == []
        shown = int(env.clock.now().timestamp() * 1000)
        env.clock.advance_sync(3_500)
        await env.call_manager().start(out.task_id, True, out.task_card.card_version)
        acted = int(env.clock.now().timestamp() * 1000)
        actions = [e.action for e in env.trace.events(task_id=out.task_id) if e.event_type == "user_action"]
        assert [a["name"] for a in actions] == ["start_confirmed"]
        assert actions[0]["shown_ms"] == shown
        assert actions[0]["acted_ms"] == acted
        assert env.trace.metrics(out.task_id).user_active_s == pytest.approx(3.5)
    finally:
        env.close()
