"""B-r2: any menu key interrupts consecutive timeouts (AC-83)."""

import pytest

from waitless.company.config import load_company, load_scenario
from waitless.company.engine import ScenarioEngine
from waitless.company.text import TextCompany


@pytest.mark.parametrize("factory", [ScenarioEngine, TextCompany])
@pytest.mark.parametrize("menu", ["main_menu", "service_menu"])
@pytest.mark.parametrize("key", ["0", "*", "#", "9"])
def test_any_menu_key_breaks_the_timeout_streak(company_dir, factory, menu, key):
    company = load_company(company_dir, "lakeshore-energy")
    scenario = load_scenario(company_dir, company.company_id, "baseline")
    text = factory(scenario, company)
    text.start()
    if menu == "service_menu":
        text.on_digits("1")
    for _ in range(2):
        assert text.on_timeout()[-1].kind == "menu"
        assert not text.ended
        assert text.on_digits(key)[-1].kind == "menu"
        assert not text.ended
    assert text.on_timeout()[-1].kind == "menu"
    assert not text.ended
    assert text.on_timeout()[-1].kind == "hangup"
    assert text.ended


@pytest.mark.parametrize("factory", [ScenarioEngine, TextCompany])
def test_empty_input_does_not_interrupt_timeouts(company_dir, factory):
    company = load_company(company_dir, "lakeshore-energy")
    scenario = load_scenario(company_dir, company.company_id, "baseline")
    text = factory(scenario, company)
    text.start()
    assert text.on_timeout()[-1].kind == "menu"
    assert text.on_digits("") == []
    assert text.on_timeout()[-1].kind == "hangup"
    assert text.ended
