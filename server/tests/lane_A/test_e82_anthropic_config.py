"""E82（tech v5.11，用户 D29 / U50）：WAITLESS_PLANNER=anthropic 的配置校验（AC-36 的延伸）与规划器工厂。

按 tech v5.11 §5.2（E82）：
- anthropic 只供开发测试，允许 text-sim 与 live-sim；replay 下设 anthropic 是配置错误（ConfigError 列出 WAITLESS_PLANNER，
  `waitless serve` 退出码 2）。最终演示、回放录制与视频必须 bedrock（D14 不变）。
- planner=anthropic 时另外必需 WAITLESS_ANTHROPIC_API_KEY、WAITLESS_ANTHROPIC_BASE_URL、WAITLESS_ANTHROPIC_TIMEOUT_S；
  WAITLESS_PLANNER_MODEL_ID 照旧必需（Claude 模型 ID，演示设 claude-haiku-5-5，代码里不写死）。
- text-sim + anthropic 不要求 AWS_*（text-sim 只有 Bedrock 规划器用它们）；live-sim + anthropic 仍要 AWS_*（Sonic、Polly）。
- build_converse_client：anthropic → AnthropicConverseClient，`.client` 是 anthropic.AsyncAnthropic，显式带钥匙、根地址、超时，
  max_retries=0（只试一次，与 Bedrock、Codex 规划器一致）。
- `.env.example` 只列名字：三个新变量都列出。
默认测试零密钥、零网络：钥匙是测试值；构造 SDK 客户端不发请求；conftest 拦住一切出站连接。
SDK 自带的 ANTHROPIC_* 环境变量（云端容器里 ANTHROPIC_BASE_URL 可能有值）在每个测试里先清掉。
"""

import anthropic
import pytest

from waitless.bedrock import AnthropicConverseClient, build_converse_client
from waitless.cli import EXIT_CONFIG, main
from waitless.config import ConfigError, load_settings

KEY = "fixture-anthropic-key-e82-not-real"
BASE_URL = "http://planner.invalid"
MODEL = "fixture-claude-model"
TIMEOUT = "12.5"
AWS = ("AWS_REGION", "AWS_ACCESS_KEY_ID", "AWS_SECRET_ACCESS_KEY")
ANTHROPIC_VARS = ("WAITLESS_ANTHROPIC_API_KEY", "WAITLESS_ANTHROPIC_BASE_URL", "WAITLESS_ANTHROPIC_TIMEOUT_S")
REQUIRED_FOR_ANTHROPIC = ANTHROPIC_VARS + ("WAITLESS_PLANNER_MODEL_ID",)
SDK_ENV = ("ANTHROPIC_API_KEY", "ANTHROPIC_AUTH_TOKEN", "ANTHROPIC_BASE_URL", "ANTHROPIC_PROFILE")


@pytest.fixture(autouse=True)
def _no_sdk_env(monkeypatch):
    for name in SDK_ENV:
        monkeypatch.delenv(name, raising=False)


@pytest.fixture
def anthropic_env(mode_env):
    def make(mode, *, drop=(), **over):
        env = mode_env(mode, WAITLESS_PLANNER="anthropic", WAITLESS_PLANNER_MODEL_ID=MODEL,
                       WAITLESS_ANTHROPIC_API_KEY=KEY, WAITLESS_ANTHROPIC_BASE_URL=BASE_URL,
                       WAITLESS_ANTHROPIC_TIMEOUT_S=TIMEOUT)
        for name in drop:
            env.pop(name, None)
        env.update(over)
        return env

    return make


def _serve(monkeypatch, env):
    """只用 env 里的值跑 `waitless serve`（只用于配置出错的情形）。conftest 已清掉全部 WAITLESS_* 与 AWS 凭证；
    进程里可能还有 AWS_REGION 等，也先清掉。万一配置被当成齐全，uvicorn.run 直接让测试失败，不真的起服务。"""
    import uvicorn

    def _no_server(*_a, **_k):
        raise AssertionError("configuration unexpectedly accepted: serve tried to start")

    monkeypatch.setattr(uvicorn, "run", _no_server)
    for name in AWS + ("AWS_DEFAULT_REGION", "AWS_SESSION_TOKEN", "AWS_PROFILE"):
        monkeypatch.delenv(name, raising=False)
    for name, value in env.items():
        monkeypatch.setenv(name, value)
    return main(["serve"])


def test_e82_text_sim_anthropic_loads_without_aws(anthropic_env):
    env = anthropic_env("text-sim", drop=AWS)
    assert not set(env) & set(AWS)

    settings = load_settings(env)

    assert settings.mode == "text-sim"
    assert settings.planner == "anthropic"
    assert settings.planner_model_id == MODEL
    assert settings.anthropic_api_key.get_secret_value() == KEY
    assert settings.anthropic_base_url == BASE_URL
    assert settings.anthropic_timeout_s == 12.5


def test_e82_live_sim_anthropic_loads_with_aws_for_sonic_and_polly(anthropic_env):
    settings = load_settings(anthropic_env("live-sim"))

    assert settings.mode == "live-sim"
    assert settings.planner == "anthropic"
    assert settings.planner_model_id == MODEL
    assert settings.aws_region == "us-east-1"


@pytest.mark.parametrize("var", REQUIRED_FOR_ANTHROPIC)
def test_e82_text_sim_anthropic_missing_variable_is_named(anthropic_env, monkeypatch, capfd, var):
    """text-sim 去掉 AWS_* 之后，缺的只能是这一个变量：AWS_* 不该出现在缺项里。"""
    env = anthropic_env("text-sim", drop=AWS + (var,))

    with pytest.raises(ConfigError) as err:
        load_settings(env)
    assert err.value.missing == (var,)

    assert _serve(monkeypatch, env) == EXIT_CONFIG == 2
    assert var in capfd.readouterr().err


@pytest.mark.parametrize("var", REQUIRED_FOR_ANTHROPIC + AWS)
def test_e82_live_sim_anthropic_missing_variable_is_named(anthropic_env, monkeypatch, capfd, var):
    env = anthropic_env("live-sim", drop=(var,))

    with pytest.raises(ConfigError) as err:
        load_settings(env)
    assert var in err.value.missing

    assert _serve(monkeypatch, env) == EXIT_CONFIG == 2
    assert var in capfd.readouterr().err


def test_e82_anthropic_is_rejected_in_replay(anthropic_env, monkeypatch, capfd):
    env = anthropic_env("replay")

    with pytest.raises(ConfigError) as err:
        load_settings(env)
    assert "WAITLESS_PLANNER" in err.value.missing

    assert _serve(monkeypatch, env) == EXIT_CONFIG == 2
    assert "WAITLESS_PLANNER" in capfd.readouterr().err


@pytest.mark.parametrize("mode", ["text-sim", "live-sim"])
def test_e82_factory_builds_anthropic_client_from_settings(anthropic_env, mode):
    settings = load_settings(anthropic_env(mode))

    planner = build_converse_client(settings)

    assert isinstance(planner, AnthropicConverseClient)
    sdk = planner.client
    assert isinstance(sdk, anthropic.AsyncAnthropic)
    assert sdk.api_key == KEY
    assert str(sdk.base_url).rstrip("/") == BASE_URL
    assert sdk.timeout == 12.5
    assert sdk.max_retries == 0


def test_e82_env_example_lists_the_anthropic_variables(repo_root):
    lines = (repo_root / ".env.example").read_text(encoding="utf-8").splitlines()
    for name in ANTHROPIC_VARS:
        assert name + "=" in lines, name
