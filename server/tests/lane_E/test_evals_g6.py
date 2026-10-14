"""E6-1: reject replay audio evaluation before it can enter the live path."""

import pytest

from e_support.evalkit import fast_engine, replay_settings, text_settings
from waitless import cli
from waitless.evals.runner import run_eval


async def test_replay_mode_rejects_audio_without_going_live(
    tmp_path, monkeypatch, mode_env, engine_config, skills_dir
):
    called = []

    async def fake_check_live(scenario, settings):
        called.append((scenario, settings.mode))
        raise RuntimeError("live path reached")

    monkeypatch.setattr(cli, "check_live", fake_check_live)
    empty_replays = tmp_path / "replays"
    empty_replays.mkdir()
    settings = replay_settings(
        mode_env, fast_engine(engine_config), tmp_path / "r", empty_replays, "missing.jsonl"
    )
    menus = tmp_path / "menus"
    menus.mkdir()

    with pytest.raises(ValueError, match="Replay.*audio"):
        await run_eval(
            "replay", settings=settings, skills_dir=skills_dir, menus_dir=menus,
            scenario="baseline", audio=True,
        )
    assert called == []


async def test_text_sim_audio_still_runs_live_sim(
    tmp_path, monkeypatch, mode_env, engine_config, skills_dir
):
    called = []

    async def fake_check_live(scenario, settings):
        called.append((scenario, settings.mode))
        raise RuntimeError("live path reached")

    monkeypatch.setattr(cli, "check_live", fake_check_live)
    settings = text_settings(mode_env, fast_engine(engine_config), tmp_path / "t")
    menus = tmp_path / "menus"
    menus.mkdir()

    with pytest.raises(RuntimeError, match="live path reached"):
        await run_eval(
            "text-sim", settings=settings, skills_dir=skills_dir, menus_dir=menus,
            scenario="baseline", audio=True,
        )
    assert called == [("baseline", "live-sim")]
