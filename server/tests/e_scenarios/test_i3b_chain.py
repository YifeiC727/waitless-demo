"""I-3b 补测 · 整条 text-sim 链（第 3 步可见组，冻结）：AC-85、AC-86 在场景层面的三处行为（test-gaps G07、G02、G09）。

经 build_services 组装 A、B、D、E、G 的真实模块，只有 Jev 与 Converse 是替身（e_support/integration.py）；时钟用 FakeClock。
- G07：baseline 押金在本人窗口内决定，接回直接确认；account-holder-required 接回后仍决策押金。
  （baseline 那一组在 I-1 集成提交上已绿：E-F5 已在 lane/e 修好，这里是防回退；account-holder-required 要等 B 第三批的场景文件。）
- G02：account-holder-required 的用户从转接返回的方式是直接断开、不点 Return——经 D 的 handoff_endpoint，断开后满
  engine.handoff.resume_grace_ms 才自动接回（AC-78、R24 的挂断路径），之后照常走到 confirmed、打扰 1 次。
- G09：menu-reworded 里 Waitless 先说「请稍等，我去问账户持有人」，坐席要等收到转达的选择才报确认号。只有公司侧看得出来：
  这里在 B 的 TextCompany 的公开方法 on_speech 外面包一层记录（照常转交，不改行为），看公司听到每句话之后说了什么。
"""

import asyncio
import re
import types

import pytest

from e_support import REAL_GUARD_S, REQUEST_TEXT, FakeHandoffSocket, build_settings
from e_support.integration import build_stack, read_scenario, run_scenario, scenario_settings
from waitless import models as m

DEPOSIT = "answer_precall:deposit_or_autopay"
HELLO = {"type": "hello", "format": "pcm_s16le", "sample_rate": 16000, "channels": 1}


def _intent(e: m.TraceEvent) -> str | None:
    return (e.policy_decision or {}).get("intent")


@pytest.mark.parametrize("scenario_id", ["baseline", "account-holder-required"])
async def test_ac85_i3b_after_the_return_the_deposit_question_is_decided(
    scenario_id, tmp_path, mode_env, engine_config, test_profile, test_account
):
    settings = scenario_settings(build_settings, mode_env, engine_config, tmp_path / scenario_id)
    stack = build_stack(settings, scenario_id=scenario_id, profile=test_profile, account=test_account)
    run = await run_scenario(stack, scenario_id)

    assert run.case.outcome == "confirmed", run.phases
    number = run.case.confirmation_number
    assert number
    ev = run.events
    types_ = [e.event_type for e in ev]
    ended_at = types_.index("handoff_ended")
    first_injection = next(e for e in ev[ended_at:] if e.event_type == "injection_sent")
    assert (first_injection.action or {}).get("template") == "handoff.resume", "Waitless resumes before anything else"
    decided = [i for i, e in enumerate(ev) if e.event_type == "decision" and _intent(e) == DEPOSIT]
    confirm_at = next(i for i, e in enumerate(ev) if e.event_type == "company_turn" and number in (e.transcript or ""))
    if scenario_id == "baseline":
        # Text-only smoke assumes the holder handled the private deposit window.
        assert decided == [], "baseline has no task-card deposit decision after return"
        assert confirm_at > ended_at
    else:
        assert decided, "the representative's deposit-or-autopay question after the return was never decided"
        d = ev[decided[0]]
        assert decided[0] > ended_at
        answer_at = next(i for i, e in enumerate(ev) if e.event_type == "injection_sent" and e.decision_id == d.decision_id)
        assert answer_at < confirm_at, "the task-card answer is given before the confirmation number"
    (summary,) = [c for c in run.case.calls if c.call_id == run.call_id]
    assert summary.interrupts_used == 1


async def test_ac85_i3b_account_holder_required_disconnect_resumes_after_the_grace(
    tmp_path, mode_env, engine_config, test_profile, test_account
):
    from fakes import FakeClock
    from waitless.call.handoff import handoff_endpoint

    sid = "account-holder-required"
    grace = engine_config.handoff.resume_grace_ms
    clock = FakeClock()
    settings = scenario_settings(build_settings, mode_env, engine_config, tmp_path / sid)
    stack = build_stack(settings, scenario_id=sid, profile=test_profile, account=test_account, clock=clock)

    prepared = await stack.parts.tasks.prepare(REQUEST_TEXT)
    card = prepared.task_card
    if prepared.status != "ready" and prepared.missing:
        values = stack.profile.model_dump()
        fields = {f.key: values.get(f.key) for f in prepared.missing if values.get(f.key)}
        card = (await stack.parts.tasks.update(prepared.task_id, m.UpdateTaskFields(**fields))).task_card
    task_id = prepared.task_id
    out = await stack.parts.calls.start(task_id, True, card.card_version, scenario_id=sid)

    async def until(pred, what, limit_ms=300_000):
        waited = 0
        while not pred():
            assert waited < limit_ms, f"{what} never happened; phase {stack.parts.calls.status(task_id).phase}"
            await clock.advance(100)
            waited += 100

    def ended_events():
        return [e for e in stack.trace.events(call_id=out.call_id) if e.event_type == "handoff_ended"]

    await asyncio.wait_for(until(lambda: stack.parts.calls.status(task_id).phase == "handoff_ready", "handoff_ready"), REAL_GUARD_S)
    joined = await stack.parts.calls.join(task_id)
    ws = FakeHandoffSocket()
    ws.app = types.SimpleNamespace(state=types.SimpleNamespace(services=stack.services))  # type: ignore[attr-defined]
    ws.query_params = {"token": joined.token}  # type: ignore[attr-defined]
    ws.feed_json(HELLO)
    endpoint = asyncio.create_task(handoff_endpoint(ws, joined.handoff_id))
    await asyncio.wait_for(until(lambda: any(f.get("type") == "ready" for f in ws.sent_json()), "ready frame"), REAL_GUARD_S)
    assert stack.parts.calls.status(task_id).phase == "in_handoff"

    ws.feed_disconnect()  # 用户直接挂断转接，不点 Return（本测试从不调用 return_）
    for _ in range(50):  # 让端点处理断开、开始计时
        await asyncio.sleep(0)
    await clock.advance(grace - 300)
    assert stack.parts.calls.status(task_id).phase == "in_handoff", "the call waits resume_grace_ms before resuming"
    assert ended_events() == []
    await asyncio.wait_for(until(lambda: bool(ended_events()), "automatic resume", limit_ms=grace + 5_000), REAL_GUARD_S)
    await asyncio.wait_for(endpoint, REAL_GUARD_S)

    await asyncio.wait_for(until(lambda: stack.parts.calls.status(task_id).ended, "call end"), REAL_GUARD_S)
    session = stack.services.calls.get_session(out.call_id)
    if session is not None:
        await asyncio.wait_for(session.wait_ended(), REAL_GUARD_S)
    case = stack.parts.results.case_result(task_id)
    for _ in range(300):
        if case.outcome != "in_progress":
            break
        await clock.advance(200)
        case = stack.parts.results.case_result(task_id)

    ev = stack.trace.events(call_id=out.call_id)
    after = ev[[e.event_type for e in ev].index("handoff_ended"):]
    first_injection = next(e for e in after if e.event_type == "injection_sent")
    assert (first_injection.action or {}).get("template") == "handoff.resume"
    expected = read_scenario(settings.company_dir, sid).expected
    assert case.outcome == expected.outcome == "confirmed"
    (summary,) = [c for c in case.calls if c.call_id == out.call_id]
    assert summary.interrupts_used == expected.interrupts == 1


async def test_ac86_i3b_the_representative_hears_the_choice_before_the_confirmation(
    tmp_path, monkeypatch, mode_env, engine_config, test_profile, test_account
):
    from waitless.company.text import TextCompany

    heard: list[tuple[str, list[str | None]]] = []
    original = TextCompany.on_speech

    def recording(self, text):
        lines = original(self, text)
        heard.append((text, [ln.confirmation_number for ln in lines]))
        return lines

    monkeypatch.setattr(TextCompany, "on_speech", recording)
    sid = "menu-reworded"
    settings = scenario_settings(build_settings, mode_env, engine_config, tmp_path / sid)
    stack = build_stack(settings, scenario_id=sid, profile=test_profile, account=test_account)
    run = await run_scenario(stack, sid)

    assert run.case.outcome == "confirmed" and len(run.answers) == 1
    _, option, _ = run.answers[0]
    relay_at = [i for i, (text, _) in enumerate(heard) if text.startswith("The account holder chose") and option.label in text]
    assert len(relay_at) == 1, f"the representative should hear the user's choice once: {[t for t, _ in heard]}"
    said_number_at = [i for i, (_, numbers) in enumerate(heard) if any(numbers)]
    assert said_number_at, "the representative never gave a confirmation number"
    assert said_number_at[0] >= relay_at[0], (
        "the representative gave the confirmation number after hearing "
        f"{heard[said_number_at[0]][0]!r}, before the user's choice was relayed"
    )
    assert re.fullmatch(read_scenario(settings.company_dir, sid).expected.confirmation_pattern, run.case.confirmation_number or "")
