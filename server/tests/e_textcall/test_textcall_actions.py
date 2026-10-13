"""TextActionBackend 把决策翻译成文本公司的输入；TextCall 在 text 模式下实现 CallSession（可见组，冻结）。

第一组（10/14 晚文字版主线的前提）。对应：
  AC-70（TextActionBackend 与 live 同一检查：decision_id 不在 trace 里就拒绝）
  AC-87（text-sim 走与 live 同一条决策链：动作只变成文本公司的输入，转接只是状态，没有音频）
  AC-85 的前提（A-16 定稿：release 用 open_answer.<字段> 模板作答，对不上用 ask_repeat）
协作者全部是 e_support 的进程内替身，不依赖 A、B、G 的实现。
"""

import asyncio
from datetime import datetime

import pytest

from e_support import (
    SAY_TEMPLATES,
    FakeHandoffSocket,
    ScriptedCompany,
    StubLlm,
    When,
    bare_decision,
    build_settings,
    line,
    make_rig,
    queue_reactions,
    run_until,
    spec_ask_user,
    spec_end,
    spec_handoff,
    spec_press,
    spec_release,
    spec_say,
    spec_wait,
)
from waitless import models as m
from waitless.interfaces import ToolFailure, UnknownDecision

ACCOUNT = "40012345"
CONFIRM = "Your confirmation number is LE-12345. Is there anything else?"


def _rig(mode_env, engine_config, fake_clock, test_profile, company, script=(), **kw):
    settings = build_settings(mode_env("text-sim"), engine_config)
    return make_rig(
        settings=settings,
        clock=fake_clock,
        company=company,
        script=script,
        allowed_fields={**test_profile, "account_number": ACCOUNT},
        **kw,
    )


# ---------------------------------------------------------------------------
# AC-70
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "spec",
    [spec_press(ACCOUNT + "#"), spec_say("No thank you."), spec_handoff(), spec_end()],
    ids=["press", "say", "handoff", "end"],
)
async def test_ac70_text_backend_rejects_unknown_decision(spec, mode_env, engine_config, fake_clock, test_profile):
    from waitless.call.textcall import TextActionBackend

    company = ScriptedCompany([])
    rig = _rig(mode_env, engine_config, fake_clock, test_profile, company)
    session = rig.create()
    backend = TextActionBackend(call=session, deps=rig.deps, company=company)

    with pytest.raises(UnknownDecision) as err:
        await backend.execute(bare_decision("dec-not-in-trace", spec))
    assert err.value.decision_id == "dec-not-in-trace"
    assert [k for k, _ in company.inputs if k != "start"] == []
    assert rig.events("dtmf_sent", "injection_sent", "hangup", "handoff_ready", "waitless_utterance") == []
    assert rig.ended_calls == []


async def test_ac70_text_backend_executes_recorded_decision(mode_env, engine_config, fake_clock, test_profile):
    from waitless.call.textcall import TextActionBackend

    company = ScriptedCompany([])
    rig = _rig(mode_env, engine_config, fake_clock, test_profile, company)
    session = rig.create()
    backend = TextActionBackend(call=session, deps=rig.deps, company=company)
    result = rig.gate.record(spec_press(ACCOUNT + "#", intent="press_field:account_number"), task_id=rig.spec.task_id, call_id=rig.spec.call_id)

    record = await backend.execute(result)

    assert isinstance(record, m.ActionRecord)
    assert record.decision_id == result.decision_id
    assert record.action == "press"
    assert record.ok is True
    assert company.digits() == [ACCOUNT + "#"]
    (dtmf,) = rig.events("dtmf_sent")
    assert dtmf.decision_id == result.decision_id
    assert (dtmf.action or {}).get("digits") == ACCOUNT + "#"


# ---------------------------------------------------------------------------
# AC-87：动作 → 文本公司的输入
# ---------------------------------------------------------------------------


async def test_ac87_say_reaches_company_verbatim_and_is_traced(mode_env, engine_config, fake_clock, test_profile):
    say_text = "No thank you, please skip the survey."
    company = ScriptedCompany(
        [line("Before we continue, would you like to take a short survey?")],
        queue_reactions(When("speech", say_text, [line("Okay. Is there anything else?")])),
    )
    rig = _rig(
        mode_env, engine_config, fake_clock, test_profile, company,
        [("survey", spec_say(say_text)), ("anything else", spec_end())],
    )
    session = await rig.start()
    await run_until(fake_clock, lambda: session.ended, what="call end")

    say_decision = rig.gate.result_for("decline_offer")
    assert company.heard()[0] == say_text
    injections = [e for e in rig.events("injection_sent") if e.decision_id == say_decision.decision_id]
    assert [(e.action or {}).get("text") for e in injections] == [say_text]
    assert say_text in rig.texts("waitless_utterance")
    # DP-C2：注入之后用刚说出的这句核对（转写最后一行是 Waitless 刚说的话）
    checks = [req for point, req in rig.gate.point_calls if point == "said_as_decided"]
    assert len(checks) == 1
    assert checks[0].transcript[-1] == m.TranscriptLine(speaker="waitless", text=say_text)
    assert checks[0].template_vars.get(m.SAID_SUMMARY_VAR) == say_text  # A-02 / AC-72：核对的是本次注入的文字
    assert checks[0].attempt == 0


@pytest.mark.parametrize(
    ("question", "expected"),
    [
        ("What is the service address?", "The service address is {address}."),
        ("What is your favorite color?", SAY_TEMPLATES["ask_repeat"]),
    ],
    ids=["field_template", "no_cue_ask_repeat"],
)
async def test_ac85_release_answers_with_open_answer_template(question, expected, mode_env, engine_config, fake_clock, test_profile):
    """A-16 定稿：text 模式没有 Sonic 草稿；release 按 field_cues 对到字段，用 open_answer.<字段> 模板填
    allowed_fields 的值；对不上就说 ask_repeat。"""
    company = ScriptedCompany(
        [line(question)],
        queue_reactions(When("speech", None, [line("Thank you. Goodbye.", kind="hangup")])),
    )
    rig = _rig(mode_env, engine_config, fake_clock, test_profile, company, [(question, spec_release())])
    session = await rig.start()
    await run_until(fake_clock, lambda: session.ended, what="call end")

    assert company.heard() == [expected.format(address=test_profile["address"])]
    assert company.heard()[0] in rig.texts("waitless_utterance")


async def test_ac87_handoff_join_return_in_text_mode(mode_env, engine_config, fake_clock, test_profile):
    ask_dob = "For security, I need the account holder's date of birth."
    company = ScriptedCompany(
        [line(ask_dob, role="rep")],
        queue_reactions(
            When("handoff", True, [line("Thank you, the account holder is verified.", role="rep")]),
            When("handoff", False, [line(CONFIRM, role="rep", confirmation_number="LE-12345")]),
        ),
    )
    rig = _rig(
        mode_env, engine_config, fake_clock, test_profile, company,
        [("date of birth", spec_handoff()), ("confirmation number", spec_end())],
    )
    session = await rig.start()
    await run_until(fake_clock, lambda: session.status().phase == "handoff_ready", what="handoff_ready")

    st = session.status()
    assert st.handoff is not None and st.handoff.state == "ready"
    assert st.handoff.company_name == rig.spec.company_name
    assert st.interrupts_used == 1
    assert company.handoffs() == []
    # D-G-4：与 live 同序——先对坐席说 announce，再 handoff_announced → handoff_ready → handoff_started（带本次决策）
    handoff = rig.gate.result_for("request_handoff")
    assert SAY_TEMPLATES["handoff.announce"] in company.heard()
    kinds = [e.event_type for e in rig.events("handoff_announced", "handoff_ready", "handoff_started")]
    assert kinds == ["handoff_announced", "handoff_ready", "handoff_started"]
    assert rig.events("handoff_started")[0].decision_id == handoff.decision_id

    joined = await session.join()
    issued = rig.handoff_tokens.issued[-1]
    assert joined.call_id == rig.spec.call_id and joined.task_id == rig.spec.task_id
    assert joined.handoff_id == issued["handoff_id"] and issued["call_id"] == rig.spec.call_id
    assert joined.token == issued["token"]
    assert joined.expires_at == datetime.fromisoformat(issued["expires_at"])
    assert joined.ws_path == f"/ws/handoff/{joined.handoff_id}"
    assert joined.company_name == rig.spec.company_name
    assert joined.media == m.JoinMedia(ai_media="disconnected", recording=False, transcription=False)
    await fake_clock.advance(100)
    assert company.handoffs() == [True]
    st = session.status()
    assert st.phase == "in_handoff"
    assert st.transcript_tail == []

    back = await session.return_to_agent(joined.handoff_id)
    assert back.status == "resuming" and back.call_id == rig.spec.call_id
    assert back.media == m.ReturnMedia(ai_media="connected", recording=False, transcription=True)
    await run_until(fake_clock, lambda: session.ended, what="call end after return")
    assert company.handoffs() == [True, False]
    assert session.status().interrupts_used == 1
    assert rig.ended_calls == [rig.spec.call_id]
    # B-G-8：on_handoff(True) 返回的是坐席对账户持有人说的话——不进决策门、不写 company_turn；
    # on_handoff(False) 之后的台词照常逐句处理
    verified = "Thank you, the account holder is verified."
    assert verified not in [t.text for t in rig.gate.turns()]
    assert verified not in [e.transcript for e in rig.events("company_turn")]
    assert CONFIRM in [t.text for t in rig.gate.turns()]
    # D-G-3、D-G-4：handoff_ended 由 TextCall 在接回时写；转接窗口（started 与 ended 之间）里没有别的事件（AC-75）
    seqs = {e.event_type: e.seq for e in rig.events("handoff_announced", "handoff_ready", "handoff_started", "handoff_ended")}
    assert seqs["handoff_announced"] < seqs["handoff_ready"] < seqs["handoff_started"] < seqs["handoff_ended"]
    window = [e for e in rig.events() if seqs["handoff_started"] < e.seq < seqs["handoff_ended"]]
    assert window == [], f"events inside the handoff window: {[e.event_type for e in window]}"


async def test_ac87_join_and_return_refused_without_handoff(mode_env, engine_config, fake_clock, test_profile):
    company = ScriptedCompany([line("Please hold while we connect you.", kind="hold", role="ivr")])
    rig = _rig(mode_env, engine_config, fake_clock, test_profile, company, [("hold", spec_wait())])
    session = await rig.start()
    await run_until(fake_clock, lambda: len(rig.gate.round_calls) >= 1, what="first decision")

    with pytest.raises(ToolFailure) as not_ready:
        await session.join()
    assert not_ready.value.code == "handoff_not_ready"
    with pytest.raises(ToolFailure) as no_handoff:
        await session.return_to_agent("handoff-that-does-not-exist")
    assert no_handoff.value.code == "no_active_handoff"
    assert rig.handoff_tokens.issued == []
    await session.hangup("test cleanup")


async def test_ac87_end_hangs_up_and_reports_ended_once(mode_env, engine_config, fake_clock, test_profile):
    company = ScriptedCompany([line(CONFIRM, role="rep", confirmation_number="LE-12345")])
    rig = _rig(mode_env, engine_config, fake_clock, test_profile, company, [("confirmation number", spec_end())])
    session = await rig.start()
    await run_until(fake_clock, lambda: session.ended, what="call end")
    await fake_clock.advance(2000)

    end_decision = rig.gate.result_for("confirm_and_end")
    hangups = rig.events("hangup")
    assert [e.decision_id for e in hangups] == [end_decision.decision_id]
    assert rig.ended_calls == [rig.spec.call_id]
    await asyncio.wait_for(session.wait_ended(), timeout=5)
    st = session.status()
    assert st.ended is True and st.phase == "ended"
    assert [ln.text for ln in session.lines()] == [CONFIRM]


async def test_ac70_consequential_text_events_point_to_recorded_decisions(mode_env, engine_config, fake_clock, test_profile):
    """一通走过 press、say、ask_user、handoff、end 的电话：每条有后果的事件都带 decision_id，并指向 trace 里已有的
    decision 事件；interrupts_used = pending_choice_created + handoff_ready（AC-35 的计数口径）。"""
    offer = "We can start on November 1 for an extra $25, or on November 3 at no charge."
    company = ScriptedCompany(
        [line("Please enter your account number, followed by the pound key.")],
        queue_reactions(
            When("digits", ACCOUNT + "#", [line("Would you like to take a short survey?", role="rep")]),
            When("speech", "No thank you", [line(offer, role="rep")]),
            When("speech", "The account holder chose", [line("For security, I need the account holder's date of birth.", role="rep")]),
            When("handoff", False, [line(CONFIRM, role="rep", confirmation_number="LE-12345")]),
        ),
    )
    llm = StubLlm(offer_options=[{"label": "November 1 for an extra $25", "detail": "Start November 1, $25 fee"},
                                 {"label": "November 3 at no charge", "detail": "Start November 3, no fee"}])
    rig = _rig(
        mode_env, engine_config, fake_clock, test_profile, company,
        [
            ("account number", spec_press(ACCOUNT + "#", intent="press_field:account_number")),
            ("survey", spec_say("No thank you, please skip the survey.")),
            ("no charge", spec_ask_user()),
            ("date of birth", spec_handoff()),
            ("confirmation number", spec_end()),
        ],
        llm=llm,
    )
    session = await rig.start()
    await run_until(fake_clock, lambda: session.status().pending_choice is not None, what="pending choice")
    choice = session.status().pending_choice
    await session.answer(choice.choice_id, choice.options[1].id)
    await run_until(fake_clock, lambda: session.status().phase == "handoff_ready", what="handoff_ready")
    joined = await session.join()
    await fake_clock.advance(100)
    await session.return_to_agent(joined.handoff_id)
    await run_until(fake_clock, lambda: session.ended, what="call end")

    decisions = rig.trace.decision_ids()
    consequential = [e for e in rig.events() if e.event_type in m.CONSEQUENTIAL_EVENTS]
    for e in consequential:
        assert e.decision_id in decisions, f"{e.event_type} points to unknown decision {e.decision_id}"
    kinds = {e.event_type for e in consequential}
    assert kinds == set(m.CONSEQUENTIAL_EVENTS), f"missing {set(m.CONSEQUENTIAL_EVENTS) - kinds}"
    (started,) = rig.events("handoff_started")
    assert started.decision_id == rig.gate.result_for("request_handoff").decision_id
    assert rig.events("user_action") == [], "user_action is written only by CallManager (A-G-9)"
    (created,) = rig.events("pending_choice_created")
    assert created.decision_id == rig.gate.result_for("ask_user").decision_id
    assert rig.pending.created[0]["decision_id"] == created.decision_id
    handoff_ready = rig.events("handoff_ready")
    assert len(handoff_ready) == 1
    assert session.status().interrupts_used == len(rig.events("pending_choice_created")) + len(handoff_ready) == 2


@pytest.mark.parametrize("jev_phase", sorted(m.CALL_PHASE_FROM_JEV))
async def test_ac87_status_phase_follows_the_shared_mapping(jev_phase, mode_env, engine_config, fake_clock, test_profile):
    """D-G-11：text 模式的 status().phase 与 live 同一规则——按最近一轮决策的 call_phase 经 CALL_PHASE_FROM_JEV 换算。"""
    company = ScriptedCompany([line("Please hold while I look that up.", role="agent")])
    rig = _rig(mode_env, engine_config, fake_clock, test_profile, company, [("Please hold", spec_wait(call_phase=jev_phase))])
    session = await rig.start()
    await run_until(fake_clock, lambda: len(rig.gate.round_calls) >= 1, what="first decision")
    await fake_clock.advance(500)
    assert session.status().phase == m.CALL_PHASE_FROM_JEV[jev_phase]
    await session.hangup("test cleanup")


async def test_ac87_attach_handoff_text_mode_sends_ready_then_ended_only(mode_env, engine_config, fake_clock, test_profile):
    """§6.4：text-sim 与 replay 下 /ws/handoff 只回 ready 和 ended；浏览器发来的音频直接丢弃、不回任何音频。
    连接由 D 的 handoff_endpoint accept，会话不再 accept（E-G-6）；连接关闭时 attach_handoff 返回（E-G-5）。"""
    company = ScriptedCompany(
        [line("For security, I need the account holder's date of birth.", role="rep")],
        queue_reactions(When("handoff", False, [line(CONFIRM, role="rep", confirmation_number="LE-12345")])),
    )
    rig = _rig(
        mode_env, engine_config, fake_clock, test_profile, company,
        [("date of birth", spec_handoff()), ("confirmation number", spec_end())],
    )
    session = await rig.start()
    await run_until(fake_clock, lambda: session.status().phase == "handoff_ready", what="handoff_ready")
    joined = await session.join()

    ws = FakeHandoffSocket()
    ws.feed_json({"type": "hello", "format": "pcm_s16le", "sample_rate": 16000, "channels": 1})
    for _ in range(3):
        ws.feed_bytes(b"\x01\x02" * (m.HANDOFF_FRAME_BYTES // 2))
    attached = asyncio.create_task(session.attach_handoff(ws, joined.handoff_id))
    await run_until(fake_clock, lambda: len(ws.sent_json()) >= 1, what="ready frame")
    ready = m.HandoffReady.model_validate(ws.sent_json()[0])
    assert ready.company_name == rig.spec.company_name

    await session.return_to_agent(joined.handoff_id)
    await run_until(fake_clock, lambda: len(ws.sent_json()) >= 2, what="ended frame after Return")
    ended = m.HandoffEnded.model_validate(ws.sent_json()[-1])
    assert ended.reason == "returned"
    ws.feed_disconnect()  # 浏览器收到 ended 后关闭
    await run_until(fake_clock, attached.done, what="attach_handoff to return once the socket closes")
    attached.result()
    assert [f["type"] for f in ws.sent_json()] == ["ready", "ended"]
    assert ws.sent_bytes() == []
    assert ws.accepted is False, "the endpoint already accepted the socket (E-G-6)"
    await run_until(fake_clock, lambda: session.ended, what="call end")


async def test_ac87_attach_handoff_returns_on_disconnect_and_leaves_resume_to_the_endpoint(mode_env, engine_config, fake_clock, test_profile):
    """E-G-5：浏览器不经 Return 就断开时 attach_handoff 立即返回；自动接回由 handoff_endpoint 计时后调 return_to_agent，
    会话自己不接回。"""
    company = ScriptedCompany(
        [line("For security, I need the account holder's date of birth.", role="rep")],
        queue_reactions(When("handoff", False, [line(CONFIRM, role="rep", confirmation_number="LE-12345")])),
    )
    rig = _rig(
        mode_env, engine_config, fake_clock, test_profile, company,
        [("date of birth", spec_handoff()), ("confirmation number", spec_end())],
    )
    session = await rig.start()
    await run_until(fake_clock, lambda: session.status().phase == "handoff_ready", what="handoff_ready")
    joined = await session.join()
    ws = FakeHandoffSocket()
    ws.feed_json({"type": "hello", "format": "pcm_s16le", "sample_rate": 16000, "channels": 1})
    attached = asyncio.create_task(session.attach_handoff(ws, joined.handoff_id))
    await run_until(fake_clock, lambda: len(ws.sent_json()) >= 1, what="ready frame")
    ws.feed_disconnect()
    await run_until(fake_clock, attached.done, max_ms=2_000, what="attach_handoff to return on disconnect")
    attached.result()
    await fake_clock.advance(rig.settings.engine.handoff.resume_grace_ms + 1_000)
    assert session.status().phase == "in_handoff"
    assert company.handoffs() == [True]

    await session.return_to_agent(joined.handoff_id)  # handoff_endpoint 计时到期后做的事
    await run_until(fake_clock, lambda: session.ended, what="call end")
    assert company.handoffs() == [True, False]
