"""B 线 · 另两个场景（10/16–10/18）：AC-85、AC-86 中 B 的部分（tech §6.10、§7.1）。

B 保证：三个场景文件都在、能被加载器读出、台词的 expect 用的是决策门认识的意图，
并且一个永远选对的 Waitless（_scenario_data.drive）照着 expect 回应时，文本公司确实走到 expected 写的结果：
按键序列 = expected.menu_digits，打扰次数 = expected.interrupts，outcome=confirmed 时拿到合格的确认号。
E 线的 run_eval / TextCall 读的就是这些字段（接口是场景 YAML 的 expected）。
"""

import re
from pathlib import Path

import pytest

from waitless.company.config import SCENARIO_IDS, list_scenarios, load_company, load_scenario
from waitless.company.text import TextCompany
from waitless.models import Scenario

from ._scenario_data import (
    COMPANY_ID,
    CONFIRMATION_PATTERN,
    SCENARIOS,
    drive,
    intent_is_known,
    is_ask_user_line,
    is_handoff_line,
    menu_steps,
    model_scenario,
)

SPEC_INTERRUPTS = {"baseline": 1, "menu-reworded": 1}  # §6.10：baseline 转接 1 次；menu-reworded ask_user 1 次


def test_ac85_scenarios_all_three_exist_and_load(company_dir: Path) -> None:
    assert set(SCENARIO_IDS) == set(SCENARIOS)
    assert sorted(list_scenarios(company_dir, COMPANY_ID)) == sorted(SCENARIOS)
    for sid in SCENARIOS:
        s = load_scenario(company_dir, COMPANY_ID, sid)
        assert isinstance(s, Scenario)
        assert (s.scenario_id, s.company_id) == (sid, COMPANY_ID)
        assert s.max_call_seconds <= 300


@pytest.mark.parametrize("sid", SCENARIOS)
def test_ac85_scenarios_expected_and_scripts_are_usable(company_dir: Path, sid: str) -> None:
    s = model_scenario(company_dir, sid)
    re.compile(s.expected.confirmation_pattern)
    if s.expected.outcome == "confirmed":
        assert s.expected.confirmation_pattern == CONFIRMATION_PATTERN
    if sid in SPEC_INTERRUPTS:
        assert s.expected.interrupts == SPEC_INTERRUPTS[sid]
    else:
        assert s.expected.interrupts >= 1, "the account holder joins at least once"
    talking = [st for st in s.steps.values() if st.kind in ("ai_agent", "live_rep")]
    assert any(st.kind == "live_rep" for st in talking), "every scenario reaches a live representative"
    for st in talking:
        assert st.persona, "live-sim bots need a persona"
        assert st.text_script, "text-sim needs a deterministic script"
        for ln in st.text_script:
            assert intent_is_known(ln.expect.intent), f"{sid}: {ln.say!r} expects unknown intent {ln.expect.intent!r}"


@pytest.mark.parametrize("sid", SCENARIOS)
def test_ac85_scenarios(company_dir: Path, sid: str) -> None:
    scenario = load_scenario(company_dir, COMPANY_ID, sid)
    company = load_company(company_dir, COMPANY_ID)
    tc = TextCompany(scenario, company)
    assert tc.scenario_id == sid
    run = drive(tc, menu_digits=scenario.expected.menu_digits, customer=company.customers[0])
    assert run.problem is None, run.problem
    assert run.stalled_on is None, f"{sid}: company went quiet after {run.stalled_on!r}"
    assert run.menu_presses == scenario.expected.menu_digits
    assert run.interrupts == scenario.expected.interrupts
    log = tc.step_log()
    assert log[0] == "greeting"
    if scenario.expected.outcome == "confirmed":
        assert run.confirmation is not None and re.fullmatch(scenario.expected.confirmation_pattern, run.confirmation)
        assert any(run.confirmation in ln.text for ln in run.transcript), "the number must be said out loud"
        assert scenario.steps[log[-1]].kind == "live_rep", "the confirmation comes from the representative"


def test_ac85_menu_reworded_changes_wording_and_order(company_dir: Path) -> None:
    base = model_scenario(company_dir, "baseline")
    rew = model_scenario(company_dir, "menu-reworded")
    assert rew.steps["main_menu"].text != base.steps["main_menu"].text
    assert rew.expected.menu_digits != base.expected.menu_digits, "option order changed, so the right keys differ"
    for step_id, step in menu_steps(rew).items():
        for key in step.options or {}:
            assert re.search(rf"(?<![0-9]){re.escape(key)}(?![0-9])", step.text or ""), (
                f"menu-reworded {step_id} offers {key!r} without saying it"
            )


def test_ac85_account_holder_required_rep_insists_at_once(company_dir: Path) -> None:
    s = model_scenario(company_dir, "account-holder-required")
    reps = [st for st in s.steps.values() if st.kind == "live_rep"]
    assert reps
    lines = reps[0].text_script or []
    handoff_at = [i for i, ln in enumerate(lines) if (ln.expect.intent == "request_handoff" or ln.expect.action == "handoff")]
    assert handoff_at, "the representative must require the account holder"
    later = [
        i
        for i, ln in enumerate(lines)
        if i not in handoff_at
        and ((ln.expect.intent or "").startswith("answer_precall:") or ln.expect.ask_user or ln.expect.intent == "confirm_and_end")
    ]
    assert handoff_at[0] <= 1 and all(handoff_at[0] < i for i in later), "the rep insists before anything else"


# ---------------------------------------------------------------------------
# AC-86 中 B 的部分：menu-reworded 坐席临时提出收费选项（由 ask_user 处理，打扰 1 次）
# ---------------------------------------------------------------------------


def test_ac86_pending_choice_flow_menu_reworded_offer(company_dir: Path) -> None:
    scenario = load_scenario(company_dir, COMPANY_ID, "menu-reworded")
    company = load_company(company_dir, COMPANY_ID)
    scripted = [(sid, ln) for sid, st in scenario.steps.items() for ln in (st.text_script or [])]
    asks = [(sid, ln) for sid, ln in scripted if ln.expect.ask_user or ln.expect.intent == "ask_user"]
    assert len(asks) == 1, "exactly one on-the-spot choice for the account holder"
    step_id, offer = asks[0]
    assert scenario.steps[step_id].kind == "live_rep"
    assert re.search(r"\$\d", offer.say) and " or " in offer.say, "a fee choice: a dollar amount and an alternative"
    assert not any(ln.expect.intent == "request_handoff" or ln.expect.action == "handoff" for _, ln in scripted), (
        "in menu-reworded the representative does not ask for the account holder"
    )
    run = drive(TextCompany(scenario, company), menu_digits=scenario.expected.menu_digits, customer=company.customers[0])
    assert run.problem is None and run.stalled_on is None, (run.problem, run.stalled_on)
    assert [ln.text for ln in run.offer_lines] == [offer.say], "the offer is said verbatim, so L3 can extract it"
    assert all(not is_handoff_line(ln) for ln in run.transcript)
    assert sum(1 for ln in run.transcript if is_ask_user_line(ln)) == 1
    assert run.interrupts == scenario.expected.interrupts == 1
    assert run.confirm_line is not None, "after the user's answer the representative goes on to the confirmation"
