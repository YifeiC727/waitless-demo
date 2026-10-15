import pytest
from waitless.config import ConfigError, load_settings
from waitless.cli import main, EXIT_CONFIG

LOOKUP = dict(WAITLESS_LOOKUP="anthropic", WAITLESS_ANTHROPIC_API_KEY="test-key",
              WAITLESS_ANTHROPIC_BASE_URL="https://lookup.example", WAITLESS_LOOKUP_MODEL_ID="test-lookup-model",
              WAITLESS_LOOKUP_TIMEOUT_S="40")

def test_u85_1_lookup_defaults_off(mode_env):
    s = load_settings(mode_env("text-sim"))
    assert s.lookup == "off" and s.lookup_model_id is None

def test_u85_1_anthropic_lookup_needs_its_variables(mode_env):
    with pytest.raises(ConfigError) as e:
        load_settings(mode_env("text-sim", WAITLESS_LOOKUP="anthropic"))
    assert set(e.value.missing) == set(LOOKUP) - {"WAITLESS_LOOKUP"}

def test_u85_1_anthropic_lookup_complete(mode_env):
    s = load_settings(mode_env("text-sim", **LOOKUP))
    assert (s.lookup, s.lookup_model_id, s.lookup_timeout_s) == ("anthropic", "test-lookup-model", 40.0)
    assert s.anthropic_api_key.get_secret_value() == "test-key" and s.planner == "bedrock"

def test_u85_1_unknown_lookup_value(mode_env):
    with pytest.raises(ConfigError) as e:
        load_settings(mode_env("text-sim", WAITLESS_LOOKUP="web"))
    assert e.value.missing == ("WAITLESS_LOOKUP",)

def test_u85_1_replay_rejects_lookup(mode_env):
    with pytest.raises(ConfigError) as e:
        load_settings(mode_env("replay", WAITLESS_LOOKUP="anthropic"))
    assert "WAITLESS_LOOKUP" in e.value.missing

def test_u85_1_recording_rejects_lookup(mode_env, tmp_path):
    with pytest.raises(ConfigError) as e:
        load_settings(mode_env("text-sim", **LOOKUP, WAITLESS_RECORD_DIR=str(tmp_path)))
    assert "WAITLESS_LOOKUP" in e.value.missing

@pytest.mark.parametrize("value", ["0", "-1", "nan", "inf"])
def test_u85_1_timeout_must_be_positive_and_finite(mode_env, value):
    with pytest.raises(ConfigError) as e:
        load_settings(mode_env("text-sim", **(LOOKUP | {"WAITLESS_LOOKUP_TIMEOUT_S": value})))
    assert "WAITLESS_LOOKUP_TIMEOUT_S" in e.value.missing

def test_u85_1_eval_refuses_lookup(mode_env, monkeypatch):
    import waitless.evals.runner
    monkeypatch.setattr(waitless.evals.runner, "run_eval", lambda *a, **kw: pytest.fail("eval called"))
    for k,v in mode_env("text-sim", **LOOKUP).items():
        monkeypatch.setenv(k, v)
    assert main(["eval", "--mode", "text-sim"]) == EXIT_CONFIG == 2

def test_u85_1_env_example_lists_lookup_variables(repo_root):
    lines = (repo_root / ".env.example").read_text().splitlines()
    for key in ("WAITLESS_LOOKUP", "WAITLESS_LOOKUP_MODEL_ID", "WAITLESS_LOOKUP_TIMEOUT_S"):
        assert key + "=" in lines
