"""AC-86：坐席临时提出收费选项时生成一张待决选项卡，选项由 Nova 2 Lite（L3）从原话原样抽出（最多 3 个）；
用户作答后坐席收到该选择；这通电话的打扰次数为 1（可见组，冻结）。

第二组（10/16–10/18）。
- 单元层：TextActionBackend 的 ask_user 动作只用 e_support 替身（L3、待决选项登记、公司）。
- 整条链：menu-reworded 场景，A、B、G 的真实模块 + FakeJev/FakeConverse（等 A、B、G 合并后变绿）。
"""

from e_support import (
    SAY_TEMPLATES,
    ScriptedCompany,
    StubLlm,
    When,
    build_settings,
    line,
    make_rig,
    queue_reactions,
    run_until,
    spec_ask_user,
    spec_end,
)
from waitless import models as m

OFFER = "We can start on November 1 for an extra $25, or on November 3 at no charge."
OPTIONS = [
    {"label": "November 1 for an extra $25", "detail": "start on November 1 for an extra $25"},
    {"label": "November 3 at no charge", "detail": "on November 3 at no charge"},
]
CONFIRM = "Great, November 3 it is. Your confirmation number is LE-24680."


async def test_ac86_ask_user_extracts_options_and_relays_the_answer(mode_env, engine_config, fake_clock, test_profile):
    company = ScriptedCompany(
        [line(OFFER, role="rep")],
        queue_reactions(When("speech", "The account holder chose", [line(CONFIRM, role="rep", confirmation_number="LE-24680")])),
    )
    llm = StubLlm(offer_options=OPTIONS)
    settings = build_settings(mode_env("text-sim"), engine_config)
    rig = make_rig(
        settings=settings,
        clock=fake_clock,
        company=company,
        script=[("no charge", spec_ask_user()), ("confirmation number", spec_end())],
        allowed_fields=dict(test_profile),
        llm=llm,
    )
    session = await rig.start()
    await run_until(fake_clock, lambda: session.status().pending_choice is not None, what="pending choice")

    ask = rig.gate.result_for("ask_user")
    assert llm.offer_calls == [OFFER], "L3 gets the company's own words for this turn"
    (created,) = rig.pending.created
    assert created["call_id"] == rig.spec.call_id
    assert created["decision_id"] == ask.decision_id
    assert created["question_text"] == OFFER
    assert [(o.label, o.detail) for o in created["options"]] == [(o["label"], o["detail"]) for o in OPTIONS]
    assert created["timeout_s"] == 20  # policy global.ask_user.timeout_sec

    st = session.status()
    assert st.phase == "awaiting_user"
    assert st.pending_choice.choice_id == created["choice"].choice_id
    assert st.pending_choice.status == "open"
    assert st.pending_choice.question_text == OFFER
    assert st.interrupts_used == 1
    (ev,) = rig.events("pending_choice_created")
    assert ev.decision_id == ask.decision_id
    assert not any(t.startswith("The account holder chose") for t in company.heard()), "nothing is relayed before the user answers"

    chosen = st.pending_choice.options[1]
    out = await session.answer(st.pending_choice.choice_id, chosen.id)
    assert out == m.AnswerChoiceOut(task_id=rig.spec.task_id, choice_id=st.pending_choice.choice_id, status="choice_recorded", option_id=chosen.id)
    await run_until(fake_clock, lambda: session.ended, what="call end")

    relayed = SAY_TEMPLATES["pending.answer"].format(option_label=chosen.label, option_detail=chosen.detail)
    assert company.heard().count(relayed) == 1
    assert rig.pending.answered == [(st.pending_choice.choice_id, chosen.id)]
    assert rig.events("user_answered")
    assert session.status().interrupts_used == 1


async def test_ac86_menu_reworded_offer_becomes_one_pending_choice(tmp_path, mode_env, engine_config, company_dir, test_profile, test_account):
    """AC-86 原文：menu-reworded 场景，坐席临时提出收费选项时生成一张待决选项卡，选项由 Lite 从原话原样抽出（最多 3 个）；
    用户作答后坐席收到该选择；这通电话的打扰次数为 1。"""
    from e_support.integration import build_stack, read_scenario, run_scenario, scenario_settings

    expected = read_scenario(company_dir, "menu-reworded").expected
    settings = scenario_settings(build_settings, mode_env, engine_config, tmp_path / "menu-reworded")
    stack = build_stack(settings, scenario_id="menu-reworded", profile=test_profile, account=test_account)
    run = await run_scenario(stack, "menu-reworded")

    assert len(run.answers) == 1, f"expected exactly one pending choice, phases {run.phases}"
    choice, option, answered = run.answers[0]
    assert answered.status == "choice_recorded"
    l3_calls = stack.fake_converse.calls_for("extract_offer_options")
    assert len(l3_calls) == 1
    assert stack.oracle.offer_sources[-1] is not None, "L3 was given the representative's offer line"
    assert choice.question_text == stack.oracle.offer_sources[-1]
    assert 1 <= len(choice.options) <= 3
    for opt in choice.options:
        assert opt.label in choice.question_text, f"option {opt.label!r} is not taken verbatim from the offer"
    assert any(option.label in said for said in run.said), "the representative was told the user's choice"
    (summary,) = [c for c in run.case.calls if c.call_id == run.call_id]
    assert summary.interrupts_used == 1
    assert run.case.outcome == expected.outcome
