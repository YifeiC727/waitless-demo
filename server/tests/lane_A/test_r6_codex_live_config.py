"""E58 / D18: Codex development planner requirements differ by mode."""

import pytest

from waitless.cli import EXIT_CONFIG, main
from waitless.config import ConfigError, load_settings


@pytest.fixture
def codex_env(mode_env):
    def make(mode, *, drop=()):
        return mode_env(
            mode,
            drop=drop,
            WAITLESS_PLANNER="codex",
            WAITLESS_CODEX_COMMAND="fixture-codex exec --model 'development planner' -",
        )

    return make


def test_live_codex_loads_without_planner_model_and_splits_command(codex_env):
    env = codex_env("live-sim", drop=("WAITLESS_PLANNER_MODEL_ID",))

    settings = load_settings(env)

    assert settings.mode == "live-sim"
    assert settings.planner == "codex"
    assert settings.planner_model_id is None
    assert settings.codex_command == ("fixture-codex", "exec", "--model", "development planner", "-")


def test_live_codex_missing_aws_secret_names_only_that_variable(codex_env, monkeypatch, capsys):
    env = codex_env("live-sim", drop=("WAITLESS_PLANNER_MODEL_ID", "AWS_SECRET_ACCESS_KEY"))

    with pytest.raises(ConfigError) as caught:
        load_settings(env)
    assert caught.value.missing == ("AWS_SECRET_ACCESS_KEY",)

    for name, value in env.items():
        monkeypatch.setenv(name, value)
    assert main(["serve"]) == EXIT_CONFIG == 2
    assert "AWS_SECRET_ACCESS_KEY" in capsys.readouterr().err


def test_replay_codex_is_still_rejected(codex_env):
    with pytest.raises(ConfigError) as caught:
        load_settings(codex_env("replay"))

    assert "WAITLESS_PLANNER" in caught.value.missing


def test_text_codex_still_loads_without_aws_or_planner_model(codex_env):
    env = codex_env("text-sim", drop=(
        "AWS_REGION", "AWS_ACCESS_KEY_ID", "AWS_SECRET_ACCESS_KEY", "WAITLESS_PLANNER_MODEL_ID",
    ))

    settings = load_settings(env)

    assert settings.mode == "text-sim"
    assert settings.planner == "codex"
    assert settings.aws_region is None
    assert settings.aws_access_key_id is None
    assert settings.aws_secret_access_key is None
    assert settings.planner_model_id is None
