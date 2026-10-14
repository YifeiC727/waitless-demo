"""Live account verification distinguishes waiting from a completed mismatch."""
import pytest

from waitless.company.config import load_company, load_scenario
from waitless.company.engine import ScenarioEngine


@pytest.fixture
def engine(company_dir):
    company = load_company(company_dir, "lakeshore-energy")
    engine = ScenarioEngine(load_scenario(company_dir, company.company_id, "baseline"), company)
    engine.start()
    engine.on_digits("1")
    engine.on_digits("1")
    assert engine.current_step == "agent"
    return engine


def assert_private_message(result):
    assert result["message"]
    assert not any(char.isdigit() for char in result["message"])
    assert "deposit" not in result["message"].lower()


def test_i2c_verify_waits_for_complete_entry(engine):
    result = engine._bot_tool("verify_account", {})
    assert result["ok"] is False
    assert result["error"] == "account_not_verified_by_keypad"
    assert result["status"] == "awaiting_keypad_entry"
    assert_private_message(result)
    assert engine._bot_on_digits("1002") is False
    assert engine._bot_tool("verify_account", {})["status"] == "awaiting_keypad_entry"


def test_i2c_completed_wrong_entry_is_mismatch(engine):
    assert engine._bot_on_digits("0#") is True
    result = engine._bot_tool("verify_account", {})
    assert result["ok"] is False
    assert result["error"] == "account_not_verified_by_keypad"
    assert result["status"] == "mismatch"
    assert_private_message(result)


def test_i2c_split_entry_verifies_and_retry_recovers(engine):
    assert engine._bot_on_digits("0#") is True
    account = engine._company.customers[0].account_number
    assert engine._bot_on_digits(account[:4]) is False
    assert engine._bot_on_digits(account[4:] + "#") is True
    assert engine._bot_tool("verify_account", {}) == {"ok": True, "step": "agent"}


@pytest.mark.parametrize("inactive", ["rep", "ended", "no_verifies"])
def test_i2c_inactive_keypad_returns_false(engine, inactive):
    if inactive == "rep":
        engine._enter("rep")
        assert engine._bot_tool("verify_account", {}) == {
            "ok": False, "error": "account_not_verified_by_keypad"}
    elif inactive == "ended":
        engine._finish()
    else:
        engine._scenario.steps[engine.current_step].verifies = None
    assert engine._bot_on_digits("100200300#") is False


def test_i2c_multiple_terminators_use_last_entry(engine):
    account = engine._company.customers[0].account_number
    assert engine._bot_on_digits(account + "#0#") is True
    assert engine._bot_tool("verify_account", {})["status"] == "mismatch"
    assert engine._bot_on_digits("") is False
