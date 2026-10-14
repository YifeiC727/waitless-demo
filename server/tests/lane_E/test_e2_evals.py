"""E 第二批 E2b 线内自测（只放第 3 步可见组没有覆盖、但本单要求的细节）：空指标、未知场景、
compare 失败时清理与坏 ref。

共用设施取第 3 步的 e_support.evalkit（冻结，不改）。零密钥零网络。"""

import pytest

from e_support import replay_header
from e_support.evalkit import MultiOracle, eval_with, fast_engine, make_skills_repo, replay_settings, scenario_case, text_settings, write_menus
from waitless import models as m


def test_compute_metrics_with_no_samples_reports_zeros():
    from waitless.evals.metrics import compute_metrics

    assert compute_metrics([]) == m.EvalMetrics(
        menu_accuracy=0.0, auto_answer_error_rate=0.0, interrupts_per_call=0.0, interrupts_per_call_max=0, task_completion_rate=0.0
    )


async def test_run_eval_rejects_an_unknown_scenario_before_any_call(tmp_path, mode_env, engine_config, company_dir, skills_dir):
    oracle = MultiOracle(company_dir)
    menus, _ = write_menus(tmp_path / "menus")
    settings = text_settings(mode_env, fast_engine(engine_config), tmp_path / "unknown")
    with pytest.raises(ValueError, match="no-such-scenario"):
        await eval_with(oracle.answer_jev, oracle.answer_converse, mode="text-sim", settings=settings, skills_dir=skills_dir,
                        menus_dir=menus, cases=[scenario_case("no-such-scenario")], scenario="no-such-scenario")


class HeadRunFailed(Exception):
    """替身 run_eval 在 head 一侧抛出的错误（不用 RuntimeError：NotImplementedError 是它的子类）。"""


async def test_compare_refs_cleans_up_and_writes_nothing_when_a_run_fails(tmp_path, monkeypatch, mode_env, engine_config, skills_dir):
    import waitless.evals.compare as compare_module
    import waitless.evals.runner as runner_module

    repo = make_skills_repo(tmp_path, skills_dir)
    replays = {}
    for ref in ("v1", "v2"):
        replays[ref] = repo / "replays" / f"eval-{ref}.jsonl"
        replays[ref].write_text(replay_header(scenario_id="baseline").model_dump_json() + "\n", encoding="utf-8")
    seen = []

    async def failing_run_eval(mode, *, skills_dir, **kwargs):
        seen.append(skills_dir)
        if (skills_dir / "decide-option" / "eval-marker.txt").exists():
            raise HeadRunFailed("head run failed")
        return m.EvalMetrics(menu_accuracy=1.0, auto_answer_error_rate=0.0, interrupts_per_call=1.0, interrupts_per_call_max=1, task_completion_rate=1.0)

    monkeypatch.setattr(runner_module, "run_eval", failing_run_eval)
    monkeypatch.setattr(compare_module, "run_eval", failing_run_eval, raising=False)
    rs = replay_settings(mode_env, engine_config, tmp_path / "cmp", repo / "replays", "eval-v1.jsonl", WAITLESS_SKILLS_DIR=str(repo / "skills"))
    with pytest.raises(HeadRunFailed):
        await compare_module.compare_refs("v1", "v2", repo_root=repo, out_dir=repo / "evals" / "results", settings=rs, replays=replays)
    assert seen and not any(p.exists() for p in seen)
    assert not (repo / "evals" / "results" / "compare.json").exists()
    with pytest.raises(ValueError):
        await compare_module.compare_refs("v1", "no-such-ref", repo_root=repo, out_dir=repo / "evals" / "results", settings=rs,
                                          replays={**replays, "no-such-ref": replays["v2"]})
    assert not (repo / "evals" / "results" / "compare.json").exists()
