"""AC-36：每种模式缺任一必需变量时，`waitless serve` 退出码 2，并在 stderr 列出变量名；replay 模式不要求 AWS 和 Jev 变量。

按 PRD 陈述完整验证（tech §7.1 只写了「缺项退出码 2」）：
- 三种模式 × 该模式的每一个必需变量（tech §5.2「必需于」列；定稿 A-13：COMPANY_HOST/PORT 只在 `waitless company` 必需）：
  load_settings 抛 ConfigError 且 missing 含该变量；`waitless serve` 返回 2，stderr 含该变量名。
- 三种模式的完整环境都能构造 Settings；replay 模式在没有任何 AWS、Jev 变量时也能构造。
- live-sim、text-sim 缺密钥时报错，不退到回放（Q6）。
- D14-1（r3）：WAITLESS_PLANNER=bedrock|codex，默认 bedrock；text-sim + codex 要 WAITLESS_CODEX_COMMAND、
  不要 AWS_* 与 WAITLESS_PLANNER_MODEL_ID（Jev 变量照旧必需）；replay 下设 codex 是 ConfigError，serve 退出码 2。
- E58（v5.3，用户 D18）：开发测试期 live-sim 也允许 codex；此时仍要 AWS_*（Sonic、Polly 用）与 WAITLESS_CODEX_COMMAND，
  不要 WAITLESS_PLANNER_MODEL_ID。
路径类变量指向临时目录，避免依赖仓库里别的线的目录是否已存在。
"""

import pytest

from waitless.cli import EXIT_CONFIG, main
from waitless.config import REQUIRED_BY_MODE, ConfigError, load_engine_config, load_settings

from a_support import ENGINE_YAML, engine_config, make_dirs

COMMON = ("WAITLESS_MODE", "WAITLESS_HOST", "WAITLESS_PORT", "WAITLESS_ALLOWED_HOSTS", "WAITLESS_DB_PATH",
          "WAITLESS_WEB_DIR", "WAITLESS_SKILLS_DIR", "WAITLESS_COMPANY_DIR", "WAITLESS_ENGINE_CONFIG")
MODELS = ("AWS_REGION", "AWS_ACCESS_KEY_ID", "AWS_SECRET_ACCESS_KEY", "WAITLESS_PLANNER_MODEL_ID", "WAITLESS_JEV_URL",
          "WAITLESS_JEV_MODEL", "WAITLESS_JEV_API_KEY")
REQUIRED = {
    "live-sim": COMMON + ("WAITLESS_CARRIER_URL", "WAITLESS_LINE_WS_URL", "WAITLESS_SONIC_MODEL_ID",
                          "WAITLESS_SONIC_VOICE") + MODELS,
    "text-sim": COMMON + MODELS,
    "replay": COMMON + ("WAITLESS_REPLAY_DIR",),
}
AWS_AND_JEV = tuple(v for v in MODELS if v.startswith("AWS_") or "JEV" in v)
CASES = [(mode, var) for mode, names in REQUIRED.items() for var in names]
ALL_VARS = sorted({v for names in REQUIRED.values() for v in names} | {
    "WAITLESS_COMPANY_HOST", "WAITLESS_COMPANY_PORT", "WAITLESS_PUBLIC_BASE_URL", "WAITLESS_RECORD_DIR",
    "WAITLESS_TEXTSIM_PACE_MS", "AWS_SESSION_TOKEN", "AWS_PROFILE", "WAITLESS_PLANNER", "WAITLESS_CODEX_COMMAND",
    "WAITLESS_CODEX_TIMEOUT_S", "WAITLESS_REPLAY_FILE", "WAITLESS_COMPANY_STATS"})
AWS_AND_PLANNER_MODEL = ("AWS_REGION", "AWS_ACCESS_KEY_ID", "AWS_SECRET_ACCESS_KEY", "WAITLESS_PLANNER_MODEL_ID")
CODEX_CMD = "fake-codex exec --model 'dev model' -"


@pytest.fixture
def full_env(mode_env, tmp_path):
    dirs = make_dirs(tmp_path / "paths")

    def make(mode, *, drop=()):
        env = mode_env(mode, WAITLESS_WEB_DIR=str(dirs["web"]), WAITLESS_SKILLS_DIR=str(dirs["skills"]),
                       WAITLESS_COMPANY_DIR=str(dirs["company"]), WAITLESS_REPLAY_DIR=str(dirs["replays"]),
                       WAITLESS_ENGINE_CONFIG=str(ENGINE_YAML))
        for name in drop:  # 先覆盖路径、再删，确保被删的变量真的不在
            env.pop(name, None)
        if mode == "replay":  # mode_env 的 replay 组本来就不含 AWS/Jev；这里只保留该模式的变量
            env = {k: v for k, v in env.items() if k in REQUIRED["replay"]}
        return env

    return make


def _use_env(monkeypatch, env):
    for name in ALL_VARS:
        monkeypatch.delenv(name, raising=False)
    for name, value in env.items():
        monkeypatch.setenv(name, value)


def test_ac36_required_by_mode_matches_tech_5_2():
    for mode, names in REQUIRED.items():
        assert set(REQUIRED_BY_MODE.get(mode, ())) == set(names), mode


@pytest.mark.parametrize("mode", list(REQUIRED))
def test_ac36_complete_env_loads(full_env, mode):
    settings = load_settings(full_env(mode))
    assert settings.mode == mode
    assert settings.engine == engine_config()


def test_ac36_replay_needs_no_aws_or_jev(full_env):
    env = full_env("replay")
    assert not set(env) & set(AWS_AND_JEV)
    settings = load_settings(env)
    assert settings.mode == "replay"
    assert REQUIRED_BY_MODE.get("replay") and not set(REQUIRED_BY_MODE["replay"]) & set(AWS_AND_JEV)


@pytest.mark.parametrize(("mode", "var"), CASES, ids=[f"{m}-{v}" for m, v in CASES])
def test_ac36_missing_variable_raises_config_error(full_env, mode, var):
    with pytest.raises(ConfigError) as err:
        load_settings(full_env(mode, drop=[var]))
    assert var in err.value.missing


@pytest.mark.parametrize(("mode", "var"), CASES, ids=[f"{m}-{v}" for m, v in CASES])
def test_ac36_serve_exits_2_and_names_the_variable(full_env, monkeypatch, capfd, mode, var):
    _use_env(monkeypatch, full_env(mode, drop=[var]))
    code = main(["serve"])
    err = capfd.readouterr().err
    assert code == EXIT_CONFIG == 2
    assert var in err


@pytest.mark.parametrize("mode", ["live-sim", "text-sim"])
def test_ac36_missing_keys_do_not_fall_back_to_replay(full_env, mode):
    with pytest.raises(ConfigError):
        load_settings(full_env(mode, drop=["AWS_ACCESS_KEY_ID", "WAITLESS_JEV_API_KEY"]))


def test_ac36_engine_config_loads_and_missing_file_is_config_error(tmp_path):
    assert load_engine_config(ENGINE_YAML) == engine_config()
    with pytest.raises(ConfigError):
        load_engine_config(tmp_path / "no-such-engine.yaml")



def _codex_env(full_env, mode="text-sim", *, drop=(), **over):
    env = full_env(mode, drop=drop)
    env.update({"WAITLESS_PLANNER": "codex", "WAITLESS_CODEX_COMMAND": CODEX_CMD})
    env.update(over)
    return env


def test_ac36_planner_defaults_to_bedrock(full_env):
    assert load_settings(full_env("text-sim")).planner == "bedrock"


def test_ac36_codex_planner_in_text_sim_needs_no_aws(full_env):
    """D14-1：text-sim + codex 不要 AWS_* 与 WAITLESS_PLANNER_MODEL_ID；命令按 shlex 切分。"""
    env = _codex_env(full_env, drop=AWS_AND_PLANNER_MODEL)
    assert not set(env) & set(AWS_AND_PLANNER_MODEL)
    settings = load_settings(env)
    assert settings.planner == "codex"
    assert tuple(settings.codex_command) == ("fake-codex", "exec", "--model", "dev model", "-")


@pytest.mark.parametrize("var", ["WAITLESS_CODEX_COMMAND", "WAITLESS_JEV_API_KEY", "WAITLESS_JEV_URL"])
def test_ac36_codex_planner_still_requires_command_and_jev(full_env, monkeypatch, capfd, var):
    env = _codex_env(full_env, drop=AWS_AND_PLANNER_MODEL)
    env.pop(var)
    with pytest.raises(ConfigError) as err:
        load_settings(env)
    assert var in err.value.missing
    _use_env(monkeypatch, env)
    assert main(["serve"]) == EXIT_CONFIG
    assert var in capfd.readouterr().err


def test_ac36_codex_planner_is_rejected_in_replay(full_env, monkeypatch, capfd):
    """E58：codex 只供开发测试；replay（评审用的回放）下设 codex 仍是配置错误。"""
    env = _codex_env(full_env, "replay")
    with pytest.raises(ConfigError):
        load_settings(env)
    _use_env(monkeypatch, env)
    assert main(["serve"]) == EXIT_CONFIG
    assert "WAITLESS_PLANNER" in capfd.readouterr().err


def test_ac36_codex_planner_in_live_sim_needs_no_planner_model(full_env):
    """E58（D18）：live-sim + codex 不要 WAITLESS_PLANNER_MODEL_ID，其余 live-sim 必需变量照旧。"""
    env = _codex_env(full_env, "live-sim", drop=("WAITLESS_PLANNER_MODEL_ID",))
    settings = load_settings(env)
    assert settings.mode == "live-sim"
    assert settings.planner == "codex"
    assert tuple(settings.codex_command) == ("fake-codex", "exec", "--model", "dev model", "-")


@pytest.mark.parametrize("var", ["AWS_REGION", "AWS_ACCESS_KEY_ID", "AWS_SECRET_ACCESS_KEY", "WAITLESS_CODEX_COMMAND",
                                 "WAITLESS_SONIC_MODEL_ID", "WAITLESS_JEV_API_KEY"])
def test_ac36_codex_planner_in_live_sim_still_requires_aws_command_and_sonic(full_env, monkeypatch, capfd, var):
    """E58：live-sim + codex 缺 AWS（Sonic、Polly 要用）、命令、Sonic 或 Jev 变量时，ConfigError 列出该变量，serve 退出码 2。"""
    env = _codex_env(full_env, "live-sim", drop=("WAITLESS_PLANNER_MODEL_ID",))
    env.pop(var, None)
    with pytest.raises(ConfigError) as err:
        load_settings(env)
    assert var in err.value.missing
    _use_env(monkeypatch, env)
    assert main(["serve"]) == EXIT_CONFIG
    assert var in capfd.readouterr().err
