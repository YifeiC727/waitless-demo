"""U70-5: formatted customer values and new callers share keypad verification."""

import re

import pytest

from waitless.company.config import load_company, load_scenario
from waitless.company.engine import ScenarioEngine
from waitless.company.text import TextCompany
from tests.lane_B.test_r2_verification import waiting_for_digits


@pytest.fixture
def inputs(company_dir):
    company = load_company(company_dir, "lakeshore-energy")
    scenario = load_scenario(company_dir, company.company_id, "baseline")
    return scenario, company


@pytest.mark.parametrize("factory", [ScenarioEngine, TextCompany])
def test_u70_5_customer_phone_digits_verify(inputs, factory):
    scenario, company = inputs
    text, _ = waiting_for_digits(factory, scenario, company)
    assert text.on_digits(re.sub(r"\D", "", company.customers[0].phone_number) + "#")[-1].role == "rep"
    assert text.step_log() == ["greeting", "main_menu", "service_menu", "hold", "agent", "rep"]


@pytest.mark.parametrize("factory", [ScenarioEngine, TextCompany])
def test_u70_5_new_caller_phone_verifies_digit_by_digit(inputs, factory):
    scenario, company = inputs
    text, _ = waiting_for_digits(factory, scenario, company)
    for ch in "8475550123":
        assert text.on_digits(ch) == []
    assert text.on_digits("#")[-1].role == "rep"


@pytest.mark.parametrize("factory", [ScenarioEngine, TextCompany])
def test_u70_5_country_code_phone_verifies(inputs, factory):
    scenario, company = inputs
    text, _ = waiting_for_digits(factory, scenario, company)
    assert text.on_digits("18475550123#")[-1].role == "rep"


@pytest.mark.parametrize("factory", [ScenarioEngine, TextCompany])
@pytest.mark.parametrize("entry", ["555012#", "28475550123#", "847555012345#", "#"])
def test_u70_5_bad_phone_entries_repeat_the_prompt(inputs, factory, entry):
    scenario, company = inputs
    text, prompt = waiting_for_digits(factory, scenario, company)
    assert text.on_digits(entry) == [prompt]
    assert "rep" not in text.step_log()
    assert text.on_digits("8475550123#")[-1].role == "rep"


def bot_at_agent(inputs):
    scenario, company = inputs
    engine = ScenarioEngine(scenario, company)
    engine.start()
    engine.on_digits("3")
    engine.on_digits("4")
    assert engine.current_step == "agent"
    return engine


def test_u70_5_bot_mode_new_caller_verifies_and_transfers(inputs):
    engine = bot_at_agent(inputs)
    assert engine._bot_tool("record_detail", {"field": "address", "value": "x"})["ok"]
    assert engine._bot_tool("record_detail", {"field": "start_date", "value": "x"})["ok"]
    assert engine._bot_on_digits("8475550123#") is True
    assert engine._bot_tool("verify_account", {}) == {"ok": True, "step": "agent"}
    assert engine._bot_tool("transfer_to_representative", {})["ok"] is True
    assert engine.current_step == "rep"


def assert_phone_message(result):
    message = result["message"]
    assert "phone number" in message
    assert "account" not in message
    assert not any(ch.isdigit() for ch in message)
    assert "deposit" not in message.lower()


def test_u70_5_mismatch_message_names_the_field(inputs):
    engine = bot_at_agent(inputs)
    waiting = engine._bot_tool("verify_account", {})
    assert waiting["status"] == "awaiting_keypad_entry"
    assert_phone_message(waiting)
    engine._bot_on_digits("0#")
    mismatch = engine._bot_tool("verify_account", {})
    assert mismatch["status"] == "mismatch"
    assert_phone_message(mismatch)
