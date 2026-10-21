"""AC-107：AI 拿不到、也说不出 SSN、卡号、出生日期（可见组，冻结；tech §7.1 指定的文件）。

第一组（10/14 晚文字版主线的前提）。两层：

1. E 线单元层（只用 e_support 替身）：档案存储返回带诱饵值的档案（DecoyProfile），对方连着问地址、出生日期、
   SSN、卡号；TextActionBackend 说出的每句话、按的每个键、交给决策门的上下文里都没有诱饵值——
   注入文字只能来自 say-templates 模板加 CompiledCall.allowed_fields（A-16：release 用 open_answer.<字段>）。
2. 整条决策链（经 build_services 组装的 A、B、G 真实模块 + FakeJev/FakeConverse，见 e_support/integration.py）：
   档案存储读出的是带诱饵值的夹具档案，按主线跑 text-sim 的 baseline（rep 步索要出生日期），三处都搜不到诱饵值：
   compile_call 用这份档案与本通任务卡编出的 sonic_prompt；trace 里每条 injection_sent 的 text；整个任务每条
   decision 事件 jev_request 的 state（另核对 FakeJev 实际收到的 state）。
"""

from e_support import (
    DecoyProfile,
    ScriptedCompany,
    StubLlm,
    When,
    build_settings,
    find_decoys,
    line,
    make_rig,
    queue_reactions,
    run_until,
    spec_ask_user,
    spec_end,
    spec_handoff,
    spec_release,
    spec_say,
)

ACCOUNT = "40012345"


async def test_ac107_text_backend_speaks_only_allowed_fields(mode_env, engine_config, fake_clock, test_profile, decoy_sensitive):
    profile = DecoyProfile(**test_profile, **decoy_sensitive, accounts={"lakeshore-energy": ACCOUNT})
    q_address = "What is the service address?"
    q_dob = "Can you confirm the account holder's date of birth?"
    q_ssn = "What is the social security number on the account?"
    q_card = "Which card number should we put the deposit on, or do you want to pay later?"
    confirm = "Your confirmation number is LE-12345. Anything else?"
    company = ScriptedCompany(
        [line(q_address)],
        queue_reactions(
            When("speech", "service address is", [line(q_dob, role="rep")]),
            When("speech", None, [line(q_ssn, role="rep")]),
            When("speech", "provide that directly", [line(q_card, role="rep")]),
            When("speech", "The account holder chose", [line("I also need to verify the date of birth with the account holder.", role="rep")]),
            When("handoff", False, [line(confirm, role="rep", confirmation_number="LE-12345")]),
        ),
    )
    settings = build_settings(mode_env("text-sim"), engine_config)
    rig = make_rig(
        settings=settings,
        clock=fake_clock,
        company=company,
        script=[
            (q_address, spec_release()),
            (q_dob, spec_release()),
            (q_ssn, spec_say("The account holder will provide that directly.", intent="state_ai_identity")),
            (q_card, spec_ask_user()),
            ("verify the date of birth", spec_handoff()),
            ("confirmation number", spec_end()),
        ],
        allowed_fields={**test_profile, "account_number": ACCOUNT},
        profile=profile,
        llm=StubLlm(offer_options=[{"label": "pay later", "detail": "do you want to pay later"}]),
    )
    session = await rig.start()
    await run_until(fake_clock, lambda: session.status().pending_choice is not None, what="pending choice")
    choice = session.status().pending_choice
    await session.answer(choice.choice_id, choice.options[0].id)
    await run_until(fake_clock, lambda: session.status().phase == "handoff_ready", what="handoff_ready")
    joined = await session.join()
    await fake_clock.advance(100)
    await session.return_to_agent(joined.handoff_id)
    await run_until(fake_clock, lambda: session.ended, what="call end")

    # 不是空检查：地址问题按模板答出了档案里的地址
    assert f"The service address is {test_profile['address']}." in company.heard()
    places = {
        "what the company heard": company.heard(),
        "keys pressed": company.digits(),
        "injection_sent texts": rig.texts("injection_sent"),
        "waitless_utterance texts": rig.texts("waitless_utterance"),
        "RoundContext given to decide_round": [ctx for _, ctx in rig.gate.round_calls],
        "DecisionRequest given to decide": [req for _, req in rig.gate.point_calls],
        "pending choices created": [c["choice"] for c in rig.pending.created],
        "all trace events": rig.events(),
    }
    for where, hay in places.items():
        assert find_decoys(hay, decoy_sensitive) == [], f"decoy value leaked into {where}"


async def test_ac107_baseline_decoys_never_reach_prompt_injections_or_jev(tmp_path, mode_env, engine_config, decoy_sensitive, test_profile, test_account):
    from e_support.integration import build_stack, compiled_for, run_scenario, scenario_settings

    settings = scenario_settings(build_settings, mode_env, engine_config, tmp_path / "baseline")
    stack = build_stack(settings, scenario_id="baseline", profile=test_profile, account=test_account, decoys=decoy_sensitive)
    run = await run_scenario(stack, "baseline")

    # 这通电话确实走到了坐席索要出生日期、转接本人的那一步，且正常结束
    assert "handoff_ready" in run.phases, f"baseline never reached the identity handoff: {run.phases}"
    assert run.case.outcome != "in_progress"

    # 1. compile_call 用这份带诱饵的档案编出的 Sonic 提示
    compiled = compiled_for(stack, run)
    assert compiled.sonic_prompt
    assert find_decoys([compiled.sonic_prompt], decoy_sensitive) == [], "decoy value in the compiled Sonic prompt"

    # 2. 全部注入话术（以及 Waitless 说出的每一句、按的每个键）
    assert run.injected, "baseline has injected utterances (identity answer, deposit choice, goodbye)"
    for where, hay in {"injection_sent texts": run.injected, "waitless_utterance texts": run.said, "keys pressed": run.digits}.items():
        assert find_decoys(hay, decoy_sensitive) == [], f"decoy value in {where}"

    # 3. 全部 Jev 请求的 state：trace 里整个任务的 decision 事件（含 P1–P3），以及 FakeJev 实际收到的
    decisions = [e for e in run.task_events if e.event_type == "decision"]
    assert decisions
    for e in decisions:
        assert find_decoys([(e.jev_request or {}).get("state", {})], decoy_sensitive) == [], f"decoy value in Jev state: {e.decision_id}"
    assert stack.fake_jev.calls, "the decision gate asked Jev"
    for call in stack.fake_jev.calls:
        assert find_decoys([call.state], decoy_sensitive) == [], "decoy value sent to Jev"
