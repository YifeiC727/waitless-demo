"""U100: bank fixtures and deterministic conversation, without model services."""

import re

import httpx

from fakes.polly import tone_pcm16
from tests.company._live_harness import company_settings
from waitless.company.app import create_company_app
from waitless.company.config import load_company, load_scenario
from waitless.company.engine import ScenarioEngine
from waitless.company.scripted import ScriptedTurns, check_scripted_steps
from waitless.official_number import phone_numbers_in

BANK = "bluefin-harbor-bank"


def bank_engine(company_dir):
    company = load_company(company_dir, BANK)
    scenario = load_scenario(company_dir, BANK, "baseline")
    return company, scenario, ScenarioEngine(scenario, company)


def reach_rep(engine):
    engine.start()
    engine.on_digits("2")
    lines = engine.on_digits("1")
    for _ in range(4):
        lines = engine.on_speech("Reply from caller")
    assert lines[-1].step_id == "rep"
    return lines[-1]


def test_u100_bank_company_loads(company_dir):
    bank = load_company(company_dir, BANK)
    energy = load_company(company_dir, "lakeshore-energy")
    assert bank.name == "Bluefin Harbor Bank"
    assert bank.line_number == "(312) 555-0180" != energy.line_number
    for field in ("name", "phone_number", "address"):
        assert getattr(bank.customers[0], field) == getattr(energy.customers[0], field)
    assert bank.customers[0].account_number == "400500600"
    assert bank.dead_end_text.strip()


def test_u100_bank_pages_have_one_matching_number(company_dir):
    bank = load_company(company_dir, BANK)
    for page in ("index.html", "contact.html"):
        text = (company_dir / BANK / bank.website.dir / page).read_text(encoding="utf-8")
        numbers = phone_numbers_in(text)
        assert len(numbers) == 1
        assert next(iter(numbers)) == re.sub(r"\D", "", bank.line_number)
        assert "fictional" in text.lower()


def test_u100_bank_scenario_shape(company_dir):
    bank, scenario, _ = bank_engine(company_dir)
    assert scenario.max_call_seconds == 400
    assert [(key, step.kind) for key, step in scenario.steps.items()] == [
        ("greeting", "prompt"), ("main_menu", "menu"), ("card_menu", "menu"),
        ("connect", "hold"), ("agent", "ai_agent"), ("queue_notice", "prompt"),
        ("queue", "hold"), ("rep", "live_rep"),
    ]
    assert scenario.steps["queue"].seconds == 180
    for menu, digits in (("main_menu", "1239"), ("card_menu", "123")):
        for digit in digits:
            assert f"press {digit}" in scenario.steps[menu].text.lower()
    agent, rep = scenario.steps["agent"], scenario.steps["rep"]
    assert len(agent.text_script) == 5 and agent.text_script[-1].expect.intent == "wait"
    assert [line.expect.intent for line in rep.text_script] == ["ask_user", "open_answer", "confirm_and_end"]
    assert scenario.expected.model_dump() == {
        "menu_digits": ["2", "1"], "interrupts": 1, "outcome": "needs_follow_up",
        "confirmation_pattern": r"^BH-\d{5}$",
    }
    check_scripted_steps(scenario, bank)


def test_u100_bank_text_walk_reaches_reference(company_dir):
    _, _, engine = bank_engine(company_dir)
    observed = engine.start() + engine.on_digits("2") + engine.on_digits("1")
    for _ in range(12):
        if engine.ended:
            break
        observed.extend(engine.on_speech("Reply from caller"))
    assert engine.ended and observed[-1].kind == "hangup"
    speech = [line for line in observed if line.kind == "speech"]
    assert re.search(r"BH-\d{5}", speech[-1].text)
    assert speech[-1].expect.outcome == "needs_follow_up"
    assert all(line.step_id != "dead_end" for line in observed)


def test_u100_bank_scripted_rep_waits_for_reply(company_dir, engine_config):
    company, scenario, engine = bank_engine(company_dir)
    first = reach_rep(engine)
    turns = ScriptedTurns(engine=engine, scenario=scenario, company=company, engine_config=engine_config)
    turns.line_finished(first, 1000)
    for now in range(1020, 31001, 20):
        assert not turns.feed(bytes(320), now)
        assert turns.poll(now) == []
    for now in range(31100, 31301, 20):
        assert turns.feed(tone_pcm16(20, 8000), now)
    assert turns.poll(32799) == []
    second, = turns.poll(32800)
    assert second.text == scenario.steps["rep"].text_script[1].say
    assert second.expect.intent == "open_answer"


async def test_u100_two_companies_in_scripted_app(company_dir, mode_env, engine_config, tmp_path):
    env = mode_env("live-sim", company=True)
    app = create_company_app(company_settings(env, engine_config, company_dir, tmp_path), agent_mode="scripted")
    numbers = {load_company(company_dir, cid).line_number for cid in (BANK, "lakeshore-energy")}
    assert set(app.state.carrier._companies) == numbers
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url=env["WAITLESS_CARRIER_URL"], trust_env=False) as http:
        response = await http.get("/sim/scenario", params={"line_number": load_company(company_dir, BANK).line_number})
    assert response.status_code == 200
    assert response.json() == {"line_number": load_company(company_dir, BANK).line_number, "scenario_id": "baseline"}
