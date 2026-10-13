"""B 线 · 文字部分（10/14 晚前）：场景引擎与文本公司（tech §6.10、§7.1 AC-82/AC-83；interfaces.CompanyEngine）。

只用 B 线代码：load_scenario / load_company 读仓库里的 baseline，建 TextCompany（或 ScenarioEngine），
由 _scenario_data.drive() 按场景自己写的 expect 回应（一个永远选对的 Waitless）。
断言只看公开接口的返回：CompanyLine 序列、step_log()、ended、current_step、confirmation_number()。
"""

import re
from pathlib import Path

import pytest

from waitless.company.config import load_company, load_scenario
from waitless.company.engine import ScenarioEngine
from waitless.company.text import TextCompany
from waitless.models import DEAD_END_TARGET, CompanyConfig, CompanyLine, Scenario

from ._scenario_data import BASELINE_STEP_ORDER, COMPANY_ID, CONFIRMATION_PATTERN, drive, needs_response


def _load(company_dir: Path) -> tuple[Scenario, CompanyConfig]:
    """在测试体内加载（不放进 fixture），未实现时报的是测试失败而不是 setup 错误。"""
    return load_scenario(company_dir, COMPANY_ID, "baseline"), load_company(company_dir, COMPANY_ID)


def _sig(lines: list[CompanyLine]) -> list[tuple[str, str, str, str, str | None]]:
    return [(ln.step_id, ln.kind, ln.role, ln.text, ln.confirmation_number) for ln in lines]


# ---------------------------------------------------------------------------
# AC-82：baseline 的公司侧步骤顺序
# ---------------------------------------------------------------------------


def test_ac82_company_baseline_step_order(company_dir: Path) -> None:
    baseline, company = _load(company_dir)
    tc = TextCompany(baseline, company)
    run = drive(tc, menu_digits=baseline.expected.menu_digits, customer=company.customers[0])
    assert run.problem is None, run.problem
    assert run.stalled_on is None, f"company went quiet after {run.stalled_on!r} while waiting for nothing"
    assert run.confirm_line is not None, "the representative never reached the confirmation"
    assert run.menu_presses == ["1", "1"]
    assert tc.step_log() == BASELINE_STEP_ORDER


def test_ac82_company_baseline_start_is_greeting_then_main_menu(company_dir: Path) -> None:
    baseline, company = _load(company_dir)
    tc = TextCompany(baseline, company)
    assert tc.scenario_id == "baseline"
    lines = tc.start()
    assert lines, "start() must speak the greeting and the first menu"
    assert all(isinstance(ln, CompanyLine) for ln in lines)
    assert (lines[0].step_id, lines[0].kind, lines[0].role) == ("greeting", "prompt", "ivr")
    assert (lines[-1].step_id, lines[-1].kind, lines[-1].role) == ("main_menu", "menu", "ivr")
    assert lines[-1].text == baseline.steps["main_menu"].text
    assert tc.step_log() == ["greeting", "main_menu"]
    assert tc.ended is False


def test_ac82_company_baseline_menu_keys_walk_the_ivr(company_dir: Path) -> None:
    baseline, company = _load(company_dir)
    tc = TextCompany(baseline, company)
    tc.start()
    after_first = tc.on_digits("1")
    assert after_first and (after_first[-1].step_id, after_first[-1].kind) == ("service_menu", "menu")
    assert tc.step_log() == ["greeting", "main_menu", "service_menu"]
    after_second = tc.on_digits("1")
    kinds = [(ln.step_id, ln.kind) for ln in after_second]
    assert ("hold", "hold") in kinds, kinds
    assert after_second[-1].role == "agent" and after_second[-1].kind == "speech", (
        "text mode has no clock: hold must hand over to the AI agent in the same batch"
    )
    assert after_second[-1].expect is not None
    assert tc.step_log() == ["greeting", "main_menu", "service_menu", "hold", "agent"]


def test_ac82_company_baseline_engine_follows_menu_options(company_dir: Path) -> None:
    baseline, company = _load(company_dir)
    eng = ScenarioEngine(baseline, company)
    eng.start()
    assert eng.current_step == "main_menu"
    eng.on_digits("1")
    assert eng.current_step == "service_menu"
    assert eng.step_log() == ["greeting", "main_menu", "service_menu"]
    assert eng.ended is False
    assert eng.confirmation_number() is None, "no confirmation number before the confirm action"


@pytest.mark.parametrize("key", ["2", "3"])
def test_ac82_company_baseline_wrong_branch_says_dead_end_text_and_hangs_up(company_dir: Path, key: str) -> None:
    baseline, company = _load(company_dir)
    # A-17 定稿（engine.py r2 文档）：先给 CompanyLine(step_id="dead_end", kind="prompt", role="ivr", text=dead_end_text)，再给 hangup
    tc = TextCompany(baseline, company)
    tc.start()
    lines = tc.on_digits(key)
    assert len(lines) >= 2 and lines[-1].kind == "hangup", [(ln.kind, ln.text) for ln in lines]
    said = lines[-2]
    assert (said.step_id, said.kind, said.role, said.text) == (DEAD_END_TARGET, "prompt", "ivr", company.dead_end_text)
    assert tc.ended is True
    assert "service_menu" not in tc.step_log()


# ---------------------------------------------------------------------------
# AC-83：菜单连续两次超时 → 公司挂断（引擎侧；线路侧见 test_audio_carrier_line.py）
# ---------------------------------------------------------------------------


def test_ac83_menu_timeout_text_company_hangs_up_after_two(company_dir: Path) -> None:
    baseline, company = _load(company_dir)
    tc = TextCompany(baseline, company)
    tc.start()
    first = tc.on_timeout()
    assert all(ln.kind != "hangup" for ln in first), "one timeout must not hang up"
    assert tc.ended is False
    if first:
        assert (first[-1].step_id, first[-1].kind) == ("main_menu", "menu"), "after one timeout the menu is offered again"
    second = tc.on_timeout()
    assert second and second[-1].kind == "hangup", [(ln.kind, ln.text) for ln in second]
    assert tc.ended is True


def test_ac83_menu_timeout_engine_hangs_up_after_two(company_dir: Path) -> None:
    baseline, company = _load(company_dir)
    eng = ScenarioEngine(baseline, company)
    eng.start()
    first = eng.on_timeout()
    assert all(ln.kind != "hangup" for ln in first)
    assert eng.ended is False and eng.current_step == "main_menu"
    second = eng.on_timeout()
    assert second and second[-1].kind == "hangup"
    assert eng.ended is True


# ---------------------------------------------------------------------------
# AC-85（baseline 部分，供 I-1 文字版主线与 E 线评测）：确认号、打扰次数、确定性
# ---------------------------------------------------------------------------


def test_ac85_baseline_confirmation_number_spoken_and_matches_pattern(company_dir: Path) -> None:
    baseline, company = _load(company_dir)
    tc = TextCompany(baseline, company)
    run = drive(tc, menu_digits=baseline.expected.menu_digits, customer=company.customers[0])
    assert run.problem is None and run.stalled_on is None, (run.problem, run.stalled_on)
    assert run.confirmation is not None, "the confirm action must carry CompanyLine.confirmation_number"
    assert re.fullmatch(CONFIRMATION_PATTERN, run.confirmation)
    assert any(run.confirmation in ln.text for ln in run.transcript), (
        "the number must be said out loud, or L4 cannot extract it from the transcript"
    )


def test_ac85_baseline_interrupts_match_expected(company_dir: Path) -> None:
    baseline, company = _load(company_dir)
    run = drive(TextCompany(baseline, company), menu_digits=baseline.expected.menu_digits, customer=company.customers[0])
    assert run.problem is None and run.stalled_on is None, (run.problem, run.stalled_on)
    assert run.interrupts == baseline.expected.interrupts == 1


def test_ac85_baseline_text_run_is_deterministic(company_dir: Path) -> None:
    baseline, company = _load(company_dir)
    a = drive(TextCompany(baseline, company), menu_digits=baseline.expected.menu_digits, customer=company.customers[0])
    b = drive(TextCompany(baseline, company), menu_digits=baseline.expected.menu_digits, customer=company.customers[0])
    assert a.problem is None and b.problem is None
    assert _sig(a.transcript) == _sig(b.transcript), "text-sim must be deterministic (K14)"


def test_ac85_baseline_handoff_lines_go_to_the_account_holder(company_dir: Path) -> None:
    # B-G-8 定稿：on_handoff(True) 返回坐席对账户持有人说的话（不进决策门）；on_handoff(False) 以需要 Waitless 回应的一句结尾
    baseline, company = _load(company_dir)
    run = drive(TextCompany(baseline, company), menu_digits=baseline.expected.menu_digits, customer=company.customers[0])
    assert run.problem is None and run.stalled_on is None, (run.problem, run.stalled_on)
    assert run.handoff_lines, "the representative talks to the account holder while they are on the line"
    assert all(ln.role == "rep" and ln.kind == "speech" for ln in run.handoff_lines)
    assert all(ln.confirmation_number is None for ln in run.handoff_lines)
    leave = next(batch for action, batch in run.batches if action == "handoff:leave")
    assert leave and needs_response(leave[-1])
