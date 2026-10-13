"""AC-85：baseline、menu-reworded、account-holder-required 三个场景文件都存在，text-sim 各跑到 expected 里写的
outcome 和 interrupts（可见组，冻结）。

第二组（10/16–10/18）。整条链测试经 build_services 组装 A、B、G 的真实模块，只有 Jev 与 Converse 是替身
（e_support/integration.py），要等 A、B、G 合并后才会变绿。按主线 prepare_task → start_assisted_call(scenario_id=…)
跑出 TextCall，读 get_case_result 里本通的 outcome 与 interrupts_used（即 calls 表的两列，tech §7.1 AC-85）；
run_eval 把同样的比较汇总成指标，见 e_evals/test_eval_runner.py。
"""

import re

import pytest

from e_support import ScriptedCompany, build_settings, line, make_rig, run_until, spec_end, spec_wait
from e_support.integration import SCENARIO_IDS, read_scenario
from waitless import models as m


def test_ac85_three_scenario_files_exist_with_expectations(company_dir):
    for sid in SCENARIO_IDS:
        path = company_dir / "lakeshore-energy" / "scenarios" / f"{sid}.yaml"
        assert path.is_file(), f"missing scenario file {path}"
        scenario = read_scenario(company_dir, sid)
        assert scenario.scenario_id == sid
        assert scenario.company_id == "lakeshore-energy"
        assert scenario.expected.outcome in ("confirmed", "needs_follow_up", "failed")
        assert scenario.expected.interrupts >= 0


async def test_ac85_lines_returns_every_company_line_in_order(mode_env, engine_config, fake_clock, test_profile):
    """评测用 TextCall.lines() 拿到公司方说过的全部台词（带 expect），与公司实际说出的一致、顺序相同。"""
    first = line("Thank you for calling Lakeshore Energy.", kind="prompt", role="ivr", expect={"intent": "wait"})
    second = line("Your confirmation number is LE-54321.", role="rep", expect={"intent": "confirm_and_end"}, confirmation_number="LE-54321")
    company = ScriptedCompany([first, second])
    settings = build_settings(mode_env("text-sim"), engine_config)
    rig = make_rig(
        settings=settings,
        clock=fake_clock,
        company=company,
        script=[("Thank you for calling", spec_wait()), ("confirmation number", spec_end())],
        allowed_fields=dict(test_profile),
    )
    session = await rig.start()
    await run_until(fake_clock, lambda: session.ended, what="call end")

    assert session.lines() == company.emitted
    assert [ln.expect for ln in session.lines()] == [first.expect, second.expect]


@pytest.mark.parametrize("scenario_id", SCENARIO_IDS)
async def test_ac85_scenario_file_exists_and_text_sim_reaches_expected_outcome_and_interrupts(
    scenario_id, tmp_path, mode_env, engine_config, company_dir, test_profile, test_account
):
    """AC-85 原文：「baseline、menu-reworded、account-holder-required 三个场景文件都存在，text-sim 各跑到 expected
    里写的 outcome 和 interrupts」。每个场景：文件存在 → 按主线跑一通 text-sim → outcome、interrupts 与 expected 相同。"""
    from e_support.integration import build_stack, run_scenario, scenario_settings

    path = company_dir / "lakeshore-energy" / "scenarios" / f"{scenario_id}.yaml"
    assert path.is_file(), f"missing scenario file {path}"
    expected = read_scenario(company_dir, scenario_id).expected
    settings = scenario_settings(build_settings, mode_env, engine_config, tmp_path / scenario_id)
    stack = build_stack(settings, scenario_id=scenario_id, profile=test_profile, account=test_account)
    run = await run_scenario(stack, scenario_id)

    assert run.case.outcome == expected.outcome, f"{scenario_id}: phases {run.phases}"
    (summary,) = [c for c in run.case.calls if c.call_id == run.call_id]
    assert summary.interrupts_used == expected.interrupts
    assert summary.scenario_id == scenario_id
    assert run.max_interrupts_seen == expected.interrupts
    if expected.outcome == "confirmed":
        assert run.case.confirmation_number is not None
        assert re.fullmatch(expected.confirmation_pattern, run.case.confirmation_number)
    assert run.digits[: len(expected.menu_digits)] == list(expected.menu_digits)
    assert all(isinstance(e, m.TraceEvent) for e in run.events)
