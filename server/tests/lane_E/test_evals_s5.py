"""Confirmed step-5 review regressions; offline clients only."""

import asyncio

import pytest

from e_support.evalkit import (
    MultiOracle, all_ids, eval_with, fast_engine, replay_eval,
    replay_settings, scenario_case, text_settings,
)


async def test_recording_binds_task_before_planning_responses(
    tmp_path, monkeypatch, mode_env, engine_config, company_dir, skills_dir,
):
    from fakes import FakeJev
    from waitless.tasks import TaskService
    from waitless.evals.runner import run_eval

    oracle = MultiOracle(company_dir)
    observed = {}
    original = TaskService._decide

    async def capture_task(self, point, task_id, text, candidates):
        observed["task_id"] = task_id
        return await original(self, point, task_id, text, candidates)

    class EchoJev(FakeJev):
        async def ask(self, state, questions):
            response = await super().ask(state, questions)
            response.usage["request_task"] = observed["task_id"]
            return response

    monkeypatch.setattr(TaskService, "_decide", capture_task)
    settings = text_settings(mode_env, fast_engine(engine_config), tmp_path / "record")
    menus = tmp_path / "menus"
    menus.mkdir()
    cases = [scenario_case("baseline")]
    from fakes import FakeConverse
    recorded = await run_eval(
        "text-sim", settings=settings, skills_dir=skills_dir, menus_dir=menus,
        cases=cases, scenario="baseline", record_dir=tmp_path / "rec",
        jev=EchoJev(default=oracle.answer_jev), converse=FakeConverse(default=oracle.answer_converse),
    )
    path = tmp_path / "rec" / "baseline.jsonl"
    raw = path.read_text(encoding="utf-8")
    assert not any(value in raw for value in all_ids(settings.db_path))
    assert '"request_task":"{{task_id}}"' in raw
    from waitless.replay.player import ReplayPlayer
    original_next = ReplayPlayer.next

    def check_substitution(self, channel, shape):
        response = original_next(self, channel, shape)
        if channel == "jev":
            assert response["usage"]["request_task"] == observed["task_id"]
        return response

    monkeypatch.setattr(ReplayPlayer, "next", check_substitution)
    rs = replay_settings(mode_env, fast_engine(engine_config), tmp_path / "replay", path.parent, path.name)
    assert await replay_eval(rs, skills_dir=skills_dir, menus_dir=menus, cases=cases) == recorded


async def test_cancelled_eval_stops_worker_while_result_is_finalizing(
    tmp_path, monkeypatch, mode_env, engine_config, company_dir, skills_dir,
):
    from waitless.results import ResultService

    finalizing = asyncio.Event()
    workers = []

    async def blocked_finalize(self, call_id):
        workers.append(asyncio.current_task())
        finalizing.set()
        await asyncio.Event().wait()

    monkeypatch.setattr(ResultService, "finalize", blocked_finalize)
    oracle = MultiOracle(company_dir)
    settings = text_settings(mode_env, fast_engine(engine_config), tmp_path / "cancel")
    menus = tmp_path / "menus"
    menus.mkdir()
    task = asyncio.create_task(eval_with(
        oracle.answer_jev, oracle.answer_converse, mode="text-sim", settings=settings,
        skills_dir=skills_dir, menus_dir=menus, cases=[scenario_case("baseline")], scenario="baseline",
    ))
    try:
        await asyncio.wait_for(finalizing.wait(), timeout=10)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        assert all(worker.done() for worker in workers), "evaluation left a result worker using its closed store"
    finally:
        task.cancel()
        for worker in workers:
            worker.cancel()
        await asyncio.gather(task, *workers, return_exceptions=True)
