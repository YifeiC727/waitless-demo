"""I-3b 补测 · AC-93 的 live-sim 代码路径（第 3 步可见组，冻结；test-gaps G29）。

AC-93〔需密钥〕：`waitless eval --audio --scenario baseline` 在 live-sim 跑通，输出与文本级相同格式的指标。真实运行要密钥、
公司进程与模拟运营商，留给 I-3b 集成单（测试计划检查 C4）；这里把真正打电话的那一步换掉，检验 run_eval 的接线（I3B-D11）：
每个场景调用 A 的 `waitless.cli.check_live(scenario, settings)`（调用时现取，便于替换），按报告的 call_id 从 settings.db_path 的
calls 表读 outcome 与 interrupts_used，按 I3B-D8 的「完成」口径算出与文本级相同字段的 EvalMetrics。
替身 check_live 只往测试自己的数据库写一行 tasks 与一行 calls（tech §6.8 的列），返回带 call_id 的报告；零密钥零网络。
"""

import sqlite3
import types
from contextlib import closing

import pytest

from e_support import build_settings
from e_support.evalkit import MultiOracle, eval_with, write_menus
from waitless import models as m


def fake_check_live_writing(rows, seen):
    """rows: {scenario: (outcome, interrupts_used)}；返回替身 check_live，它把这一通写进 calls 表。"""

    async def fake_check_live(scenario, s):
        seen.append(scenario)
        outcome, used = rows[scenario]
        with closing(sqlite3.connect(s.db_path)) as con:
            con.execute(
                "insert into tasks (task_id, company_id, goal, request_text, task_card_json, card_version, status, created_at, updated_at)"
                " values (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (f"task-{scenario}", "lakeshore-energy", "Start electric service", "start service", "{}", 1, "completed",
                 "2026-10-16T12:00:00+00:00", "2026-10-16T12:05:00+00:00"),
            )
            con.execute(
                "insert into calls (call_id, task_id, scenario_id, mode, started_at, ended_at, duration_s, outcome, confirmation_number,"
                " skill_commits_json, interrupts_used) values (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (f"call-{scenario}", f"task-{scenario}", scenario, "live-sim", "2026-10-16T12:00:00+00:00", "2026-10-16T12:04:00+00:00",
                 240, outcome, "LE-11111", "[]", used),
            )
            con.commit()
        return types.SimpleNamespace(scenario_id=scenario, call_id=f"call-{scenario}", outcome="confirmed")

    return fake_check_live


async def test_ac93_i3b_run_eval_live_sim_reuses_check_live_and_reports_text_level_metrics(
    tmp_path, monkeypatch, mode_env, engine_config, company_dir, skills_dir
):
    import waitless.cli as cli
    from waitless.store import Store

    settings = build_settings(mode_env("live-sim", WAITLESS_DB_PATH=str(tmp_path / "live.db")), engine_config)
    Store(settings.db_path).close()
    seen: list[str] = []
    monkeypatch.setattr(cli, "check_live", fake_check_live_writing({"baseline": ("confirmed", 1)}, seen))
    oracle = MultiOracle(company_dir)
    menus, _ = write_menus(tmp_path / "menus")
    metrics = await eval_with(oracle.answer_jev, oracle.answer_converse, mode="live-sim", settings=settings, skills_dir=skills_dir,
                              menus_dir=menus, cases=[], scenario="baseline", audio=True)
    assert seen == ["baseline"], "one live-sim call per requested scenario, through cli.check_live"
    assert isinstance(metrics, m.EvalMetrics)
    assert set(metrics.model_dump()) == set(m.EvalMetrics.model_fields), "same metric fields as the text-level eval"
    assert metrics.task_completion_rate == pytest.approx(1.0)
    assert metrics.interrupts_per_call == pytest.approx(1.0) and metrics.interrupts_per_call_max == 1
