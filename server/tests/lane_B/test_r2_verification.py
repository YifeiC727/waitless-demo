"""B-r2: keypad verification uses the configured customer field."""

import pytest

from waitless.company.config import ScenarioError, load_company, load_scenario
from waitless.company.engine import ScenarioEngine
from waitless.company.text import TextCompany


@pytest.fixture
def inputs(company_dir):
    company = load_company(company_dir, "lakeshore-energy").model_copy(deep=True)
    scenario = load_scenario(company_dir, company.company_id, "baseline").model_copy(deep=True)
    return scenario, company


def waiting_for_digits(factory, scenario, company):
    text = factory(scenario, company)
    text.start()
    text.on_digits("1")
    text.on_digits("1")
    text.on_speech("Service address")
    prompt = text.on_speech("Start date")[-1]
    assert prompt.expect.intent.startswith("press_field:")
    return text, prompt


@pytest.mark.parametrize("factory", [ScenarioEngine, TextCompany])
@pytest.mark.parametrize("field,intent", [
    ("phone_number", "press_field:account_number"),
    ("account_number", "press_field:phone_number"),
    ("address", "press_field:missing_field"),
])
def test_verifies_selects_customer_field_independently_of_intent(inputs, factory, field, intent):
    scenario, company = inputs
    scenario.steps["agent"].verifies = field
    scenario.steps["agent"].text_script[2].expect.intent = intent
    company.customers[0].phone_number = "5551234"
    company.customers[0].address = "123456"
    text, prompt = waiting_for_digits(factory, scenario, company)
    wrong_field = "phone_number" if field == "account_number" else "account_number"
    assert text.on_digits(getattr(company.customers[0], wrong_field) + "#") == [prompt]
    assert "rep" not in text.step_log()
    value = getattr(company.customers[0], field)
    assert text.on_digits(value[:2]) == []
    lines = text.on_digits(value[2:] + "#")
    assert lines[-1].role == "rep"
    assert text.step_log() == ["greeting", "main_menu", "service_menu", "hold", "agent", "rep"]


@pytest.mark.parametrize("factory", [ScenarioEngine, TextCompany])
@pytest.mark.parametrize("field", ["missing_field", "model_dump", "", None])
def test_invalid_verifies_is_a_configuration_error(inputs, factory, field):
    scenario, company = inputs
    scenario.steps["agent"].verifies = field
    # Runtime rejection is permitted, as is rejection before processing input.
    with pytest.raises(ScenarioError):
        text, _ = waiting_for_digits(factory, scenario, company)
        text.on_digits(company.customers[0].account_number + "#")


@pytest.mark.parametrize("factory", [ScenarioEngine, TextCompany])
@pytest.mark.parametrize("field", ["name", "address"])
def test_pound_alone_never_verifies_even_an_empty_customer_field(inputs, factory, field):
    scenario, company = inputs
    scenario.steps["agent"].verifies = field
    scenario.steps["agent"].text_script[2].expect.intent = "press_field:" + field
    setattr(company.customers[0], field, "")
    text, prompt = waiting_for_digits(factory, scenario, company)
    assert text.on_digits("") == []
    assert text.on_digits("#") == [prompt]
    assert "rep" not in text.step_log()
    setattr(company.customers[0], field, "54321")
    assert text.on_digits("54321#")[-1].role == "rep"


@pytest.mark.parametrize("factory", [ScenarioEngine, TextCompany])
def test_baseline_uses_any_matching_customer_and_replays_nonmatches(inputs, factory):
    scenario, company = inputs
    other = company.customers[0].model_copy(update={"phone_number": "(312) 555-0148"})
    company.customers.append(other)
    text, prompt = waiting_for_digits(factory, scenario, company)
    assert text.on_digits("0#") == [prompt]
    assert text.on_digits("#") == [prompt]
    assert "rep" not in text.step_log()
    assert text.on_digits("3125550148") == []
    assert text.on_digits("#")[-1].role == "rep"
