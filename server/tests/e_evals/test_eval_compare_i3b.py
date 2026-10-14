"""I-3b 补测 · compare（第 3 步可见组，冻结）：AC-91、AC-92（test-gaps G24、G26）。

- G24：两个 ref 各用自己的回放、各用自己导出的 skills 跑真实的 run_eval（replay 模式），同一组菜单；v1 的录制菜单答错 3 个、
  v2 全对，所以 base 比 head 差、没有倒退（录制与对比都不传 cases，用例取导出的 skills 里的 cases*.jsonl，两边一致）；把两个 ref 对调后，倒退项恰好是 menu_accuracy。
- G26（I3B-D15）：compare.md 是固定的英文表格，可直接贴进提交材料：
    表头 `| Metric | <base> | <head> | Better when | Regressed |`；4 项指标按 METRIC_DIRECTIONS 的顺序各一行，
    Better when 填 higher/lower，Regressed 填 yes/no；再一行 interrupts_per_call_max（Better when 填 lower，Regressed 填
    informational，不参与倒退判定）；表下一行 `Regressions: <逗号分隔>` 或 `Regressions: none`；再有一行说明分母为 0 时的取值
    （含 denominator 一词）。
"""

import asyncio
import shutil

import pytest

from e_support.evalkit import (
    GUARD_S,
    MultiOracle,
    eval_with,
    fast_engine,
    make_skills_repo,
    md_table,
    replay_settings,
    text_settings,
    with_answers,
    write_menus,
)
from waitless import models as m


def metrics(menu=0.8, err=0.1, ipc=1.0, ipc_max=1, done=0.9) -> m.EvalMetrics:
    return m.EvalMetrics(menu_accuracy=menu, auto_answer_error_rate=err, interrupts_per_call=ipc, interrupts_per_call_max=ipc_max,
                         task_completion_rate=done)


async def test_ac91_i3b_compare_refs_runs_each_ref_on_its_own_recording(tmp_path, mode_env, engine_config, company_dir, skills_dir):
    from waitless.evals.compare import compare_refs

    repo = make_skills_repo(tmp_path, skills_dir)
    menus, right = write_menus(repo / "evals" / "menus")
    _, wrong = write_menus(tmp_path / "same-texts", wrong={0, 1, 2})
    oracle = MultiOracle(company_dir)
    recorded = {}
    for ref, answers in (("v1", wrong), ("v2", right)):
        s = text_settings(mode_env, fast_engine(engine_config), tmp_path / f"record-{ref}", WAITLESS_SKILLS_DIR=str(repo / "skills"))
        recorded[ref] = await eval_with(with_answers(oracle.answer_jev, by_text=answers), oracle.answer_converse, mode="text-sim", settings=s,
                        skills_dir=repo / "skills", menus_dir=menus, scenario="baseline", record_dir=tmp_path / f"rec-{ref}")
        shutil.copy(tmp_path / f"rec-{ref}" / "baseline.jsonl", repo / "replays" / f"eval-{ref}.jsonl")
    replays = {ref: repo / "replays" / f"eval-{ref}.jsonl" for ref in ("v1", "v2")}
    rs = replay_settings(mode_env, fast_engine(engine_config), tmp_path / "cmp", repo / "replays", "eval-v1.jsonl",
                         WAITLESS_SKILLS_DIR=str(repo / "skills"))

    report = await asyncio.wait_for(
        compare_refs("v1", "v2", repo_root=repo, out_dir=repo / "evals" / "results", settings=rs, replays=replays), timeout=GUARD_S
    )
    assert report.base == recorded["v1"], "base is replayed from eval-v1.jsonl"
    assert report.head == recorded["v2"], "head is replayed from eval-v2.jsonl"
    assert report.base.menu_accuracy < report.head.menu_accuracy
    assert report.regressions == []
    swapped = await asyncio.wait_for(
        compare_refs("v2", "v1", repo_root=repo, out_dir=tmp_path / "swapped", settings=rs, replays=replays), timeout=GUARD_S
    )
    assert swapped.regressions == ["menu_accuracy"]
    assert (repo / "evals" / "results" / "compare.json").is_file()


def test_ac91_i3b_compare_md_is_a_fixed_english_table(tmp_path):
    from waitless.evals.compare import METRIC_DIRECTIONS, CompareReport, write_report

    base = metrics(menu=0.8, err=0.1, ipc=1.0, ipc_max=1, done=0.9)
    head = metrics(menu=0.6, err=0.1, ipc=1.25, ipc_max=3, done=1.0)
    report = CompareReport(base_ref="v1", head_ref="v2", base=base, head=head, regressions=["menu_accuracy", "interrupts_per_call"])
    _, md_path = write_report(report, tmp_path)
    md = md_path.read_text(encoding="utf-8")
    table = md_table(md)
    assert table[0] == ["Metric", "v1", "v2", "Better when", "Regressed"]
    rows = {r[0]: r for r in table[1:]}
    assert [r[0] for r in table[1:5]] == list(METRIC_DIRECTIONS), "the four metrics, in METRIC_DIRECTIONS order"
    for name, direction in METRIC_DIRECTIONS.items():
        row = rows[name]
        assert float(row[1]) == pytest.approx(getattr(base, name), abs=5e-4)
        assert float(row[2]) == pytest.approx(getattr(head, name), abs=5e-4)
        assert row[3] == direction
        assert row[4] == ("yes" if name in report.regressions else "no")
    mx = rows["interrupts_per_call_max"]
    assert (int(mx[1]), int(mx[2]), mx[3], mx[4]) == (1, 3, "lower", "informational")
    regress_line = next(ln for ln in md.splitlines() if ln.strip().startswith("Regressions:"))
    assert [x.strip() for x in regress_line.split(":", 1)[1].split(",")] == ["menu_accuracy", "interrupts_per_call"]
    assert "denominator" in md.lower()


def test_ac92_i3b_compare_md_says_none_when_nothing_regressed(tmp_path):
    from waitless.evals.compare import CompareReport, write_report

    same = metrics()
    _, md_path = write_report(CompareReport(base_ref="abc1234", head_ref="def5678", base=same, head=same, regressions=[]), tmp_path)
    md = md_path.read_text(encoding="utf-8")
    table = md_table(md)
    assert table[0] == ["Metric", "abc1234", "def5678", "Better when", "Regressed"]
    assert all(r[4] in ("no", "informational") for r in table[1:])
    regress_line = next(ln for ln in md.splitlines() if ln.strip().startswith("Regressions:"))
    assert regress_line.split(":", 1)[1].strip() == "none"
