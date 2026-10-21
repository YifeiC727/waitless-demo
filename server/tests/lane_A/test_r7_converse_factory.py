"""E58-2: the planner factory must honor the development modes accepted by config."""

import pytest

from waitless.bedrock import BotoConverseClient, CodexCliConverseClient, build_converse_client
from waitless.config import load_settings


def test_live_codex_factory_accepts_settings_without_planner_model(mode_env):
    settings = load_settings(mode_env(
        "live-sim",
        drop=("WAITLESS_PLANNER_MODEL_ID",),
        WAITLESS_PLANNER="codex",
        WAITLESS_CODEX_COMMAND="fixture-codex exec --model 'development planner' -",
        WAITLESS_CODEX_TIMEOUT_S="12.5",
    ))

    client = build_converse_client(settings)

    assert isinstance(client, CodexCliConverseClient)
    assert settings.planner_model_id is None
    assert client.command == settings.codex_command
    assert client.timeout_s == settings.codex_timeout_s == 12.5


def test_text_codex_factory_keeps_default_timeout(mode_env):
    settings = load_settings(mode_env(
        "text-sim",
        drop=("AWS_REGION", "AWS_ACCESS_KEY_ID", "AWS_SECRET_ACCESS_KEY", "WAITLESS_PLANNER_MODEL_ID"),
        WAITLESS_PLANNER="codex",
        WAITLESS_CODEX_COMMAND="fixture-codex exec -",
    ))

    client = build_converse_client(settings)

    assert isinstance(client, CodexCliConverseClient)
    assert client.command == settings.codex_command
    assert client.timeout_s == 60.0


def test_replay_codex_factory_rejects_settings_bypassing_load_settings(mode_env):
    settings = load_settings(mode_env("replay")).model_copy(update={
        "planner": "codex",
        "codex_command": ("fixture-codex", "exec", "-"),
    })

    with pytest.raises(ValueError, match=r"(?i)codex.*replay"):
        build_converse_client(settings)


def test_live_bedrock_factory_still_constructs_boto_client(mode_env):
    settings = load_settings(mode_env("live-sim"))

    client = build_converse_client(settings)

    assert settings.planner == "bedrock"
    assert isinstance(client, BotoConverseClient)
    assert client.client.meta.region_name == settings.aws_region
