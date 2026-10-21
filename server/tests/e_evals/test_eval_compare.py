"""AC-91：`waitless eval compare --base-ref A --head-ref B` 对同一组用例输出两版指标对比 compare.json 与 compare.md；
AC-92：任一指标倒退时退出码 1，不倒退时 0（可见组，冻结）。

第二组（10/16–10/18）。本线测 compare_refs 本身与倒退判定 regressions()；cli 的 `eval compare` 入口与退出码
由 A 线挂上并测（tech §7.1 AC-92），I-3 提交 evals/results/ 的结果按测试计划的检查命令核对。

compare_refs 的测试在 tmp_path 里建一个只有两次提交的 git 仓库（v1、v2 两个标签，skills/ 里放一个版本标记文件），
并把 run_eval 换成按导出目录里的标记返回预设指标的替身——这样不需要回放文件和密钥，
也能从外面看出：两个 ref 的 skills/ 各被导出、各跑了一遍；工作区与 HEAD 没动；临时目录用完删除；报告写到 out_dir。
"""

import subprocess
from pathlib import Path

import pytest

from e_support import build_settings, replay_header
from waitless import models as m
from waitless.interfaces import ReplayMismatch


def metrics(menu: float = 0.8, err: float = 0.1, ipc: float = 1.0, ipc_max: int = 1, done: float = 0.9) -> m.EvalMetrics:
    return m.EvalMetrics(
        menu_accuracy=menu,
        auto_answer_error_rate=err,
        interrupts_per_call=ipc,
        interrupts_per_call_max=ipc_max,
        task_completion_rate=done,
    )


BASE = metrics()
WORSE = {
    "menu_accuracy": metrics(menu=0.7),
    "auto_answer_error_rate": metrics(err=0.2),
    "interrupts_per_call": metrics(ipc=1.5),
    "task_completion_rate": metrics(done=0.8),
}


def test_ac92_metric_directions_cover_the_four_metrics():
    from waitless.evals.compare import METRIC_DIRECTIONS

    assert METRIC_DIRECTIONS == {
        "menu_accuracy": "higher",
        "auto_answer_error_rate": "lower",
        "interrupts_per_call": "lower",
        "task_completion_rate": "higher",
    }


@pytest.mark.parametrize("metric", sorted(WORSE))
def test_ac92_regressions_follow_metric_directions(metric):
    from waitless.evals.compare import regressions

    assert regressions(BASE, WORSE[metric]) == [metric]


def test_ac92_no_regression_when_equal_or_better():
    from waitless.evals.compare import regressions

    assert regressions(BASE, BASE) == []
    better = metrics(menu=0.95, err=0.0, ipc=0.5, ipc_max=1, done=1.0)
    assert regressions(BASE, better) == []


def test_ac91_write_report_writes_json_and_md(tmp_path):
    from waitless.evals.compare import CompareReport, write_report

    report = CompareReport(base_ref="v1", head_ref="v2", base=BASE, head=WORSE["menu_accuracy"], regressions=["menu_accuracy"])
    json_path, md_path = write_report(report, tmp_path)

    assert json_path == tmp_path / "compare.json"
    assert md_path == tmp_path / "compare.md"
    assert CompareReport.model_validate_json(json_path.read_text(encoding="utf-8")) == report
    md = md_path.read_text(encoding="utf-8")
    for needle in ("v1", "v2", *m.EvalMetrics.model_fields):
        assert needle in md, f"compare.md does not mention {needle}"


# ---------------------------------------------------------------------------
# compare_refs：临时 git 仓库 + run_eval 替身
# ---------------------------------------------------------------------------

GIT_CONFIG = ["-c", "user.name=Waitless Test", "-c", "user.email=test@example.invalid", "-c", "commit.gpgsign=false", "-c", "tag.gpgsign=false"]


def git(repo: Path, *args: str) -> str:
    out = subprocess.run(["git", *GIT_CONFIG, *args], cwd=repo, capture_output=True, text=True, check=True)
    return out.stdout.strip()


def make_repo(root: Path) -> Path:
    repo = root / "repo"
    (repo / "skills" / "decide-option").mkdir(parents=True)
    (repo / "evals" / "menus").mkdir(parents=True)
    (repo / "replays").mkdir()
    (repo / "evals" / "menus" / ".keep").write_text("", encoding="utf-8")
    (repo / "replays" / ".keep").write_text("", encoding="utf-8")
    marker = repo / "skills" / "decide-option" / "eval-marker.txt"
    git(repo, "init", "-q")
    marker.write_text("v1\n", encoding="utf-8")
    git(repo, "add", "-A")
    git(repo, "commit", "-q", "-m", "skills v1")
    git(repo, "tag", "v1")
    marker.write_text("v2\n", encoding="utf-8")
    git(repo, "commit", "-q", "-am", "skills v2")
    git(repo, "tag", "v2")
    return repo


def patch_run_eval(monkeypatch, results: dict[str, m.EvalMetrics]) -> list[dict]:
    import waitless.evals.compare as compare_module
    import waitless.evals.runner as runner_module

    calls: list[dict] = []

    async def fake_run_eval(mode, *, skills_dir, menus_dir=None, **kwargs):
        skills = Path(skills_dir)
        markers = list(skills.rglob("eval-marker.txt"))
        assert len(markers) == 1, f"exported skills dir {skills} should hold one marker, found {markers}"
        version = markers[0].read_text(encoding="utf-8").strip()
        calls.append({"mode": mode, "skills_dir": skills.resolve(), "version": version, "menus_dir": str(menus_dir)})
        return results[version]

    monkeypatch.setattr(runner_module, "run_eval", fake_run_eval)
    monkeypatch.setattr(compare_module, "run_eval", fake_run_eval, raising=False)
    return calls


def replay_settings(mode_env, engine_config, repo: Path):
    """E-G-2：settings 由 cli 传入（mode=replay 的配置）。"""
    env = mode_env("replay", WAITLESS_SKILLS_DIR=str(repo / "skills"), WAITLESS_REPLAY_DIR=str(repo / "replays"))
    return build_settings(env, engine_config)


def per_ref_replays(repo: Path, *refs: str) -> dict[str, Path]:
    """E-G-2：每个版本各一份回放（I-3 用 text-sim 对每个版本各录一份，如 replays/eval-<ref>.jsonl）。"""
    out = {}
    for ref in refs:
        path = repo / "replays" / f"eval-{ref}.jsonl"
        path.write_text(replay_header(scenario_id="baseline").model_dump_json() + "\n", encoding="utf-8")
        out[ref] = path
    return out


async def test_ac91_compare_refs_runs_both_refs_and_writes_report(tmp_path, monkeypatch, mode_env, engine_config):
    """AC-91 原文：对同一组用例输出两版指标对比 compare.json 和 compare.md。"""
    from waitless.evals.compare import CompareReport, compare_refs, regressions

    repo = make_repo(tmp_path)
    head_before = git(repo, "rev-parse", "HEAD")
    calls = patch_run_eval(monkeypatch, {"v1": BASE, "v2": WORSE["task_completion_rate"]})
    out_dir = repo / "evals" / "results"

    report = await compare_refs(
        "v1", "v2", repo_root=repo, out_dir=out_dir,
        settings=replay_settings(mode_env, engine_config, repo), replays=per_ref_replays(repo, "v1", "v2"),
    )

    assert isinstance(report, CompareReport)
    assert (report.base_ref, report.head_ref) == ("v1", "v2")
    assert report.base == BASE and report.head == WORSE["task_completion_rate"]
    assert report.regressions == regressions(BASE, WORSE["task_completion_rate"]) == ["task_completion_rate"]
    assert sorted(c["version"] for c in calls) == ["v1", "v2"], "each ref's skills/ is evaluated once"
    assert {c["mode"] for c in calls} == {"replay"}
    assert len({c["menus_dir"] for c in calls}) == 1, "both versions run the same menus (同一组用例)"
    for c in calls:
        assert not c["skills_dir"].exists(), "the exported skills dir is removed afterwards"
        assert repo.resolve() not in c["skills_dir"].parents, "exports go to a temp dir, not into the work tree"
    assert git(repo, "rev-parse", "HEAD") == head_before
    assert git(repo, "status", "--porcelain", "--untracked-files=no") == ""
    assert (repo / "skills" / "decide-option" / "eval-marker.txt").read_text(encoding="utf-8").strip() == "v2"
    assert CompareReport.model_validate_json((out_dir / "compare.json").read_text(encoding="utf-8")) == report
    assert (out_dir / "compare.md").read_text(encoding="utf-8").strip()


async def test_ac91_compare_refs_refuses_a_ref_without_its_replay(tmp_path, monkeypatch, mode_env, engine_config):
    """E-G-2：缺哪个 ref 的回放就报错（ReplayMismatch，同 E 线对缺文件的约定），不改走在线、不写报告。"""
    from waitless.evals.compare import compare_refs

    repo = make_repo(tmp_path)
    calls = patch_run_eval(monkeypatch, {"v1": BASE, "v2": BASE})
    replays = per_ref_replays(repo, "v1")
    out_dir = repo / "evals" / "results"
    with pytest.raises(ReplayMismatch):
        await compare_refs(
            "v1", "v2", repo_root=repo, out_dir=out_dir,
            settings=replay_settings(mode_env, engine_config, repo), replays=replays,
        )
    assert "v2" not in [c["version"] for c in calls]
    assert not (out_dir / "compare.json").exists()


@pytest.mark.parametrize(("head", "exit_code"), [(WORSE["menu_accuracy"], 1), (BASE, 0)], ids=["regression", "no_regression"])
def test_ac92_compare_exit_code_is_1_on_regression_and_0_otherwise(head, exit_code, tmp_path, monkeypatch, mode_env):
    """AC-92 原文：任一指标倒退时 compare 退出码 1，不倒退时 0。
    经 cli 的 `waitless eval compare` 入口（A 线挂上，E-G-2 起传 settings 与 replays）；compare_refs 换成返回给定报告的替身。"""
    import waitless.cli as cli
    import waitless.evals.compare as compare_module
    from waitless.evals.compare import CompareReport, regressions

    repo = make_repo(tmp_path)
    per_ref_replays(repo, "v1", "v2")
    for name, value in mode_env("replay", WAITLESS_SKILLS_DIR=str(repo / "skills"), WAITLESS_REPLAY_DIR=str(repo / "replays")).items():
        monkeypatch.setenv(name, value)
    monkeypatch.chdir(repo)

    async def fake_compare_refs(base_ref, head_ref, **kwargs):
        return CompareReport(base_ref=base_ref, head_ref=head_ref, base=BASE, head=head, regressions=regressions(BASE, head))

    monkeypatch.setattr(compare_module, "compare_refs", fake_compare_refs)
    monkeypatch.setattr(cli, "compare_refs", fake_compare_refs, raising=False)
    assert cli.main(["eval", "compare", "--base-ref", "v1", "--head-ref", "v2"]) == exit_code
