import re

import yaml

from tests.company._scenario_data import drive
from waitless.company.config import load_company, load_scenario
from waitless.company.engine import ScenarioEngine
from waitless.company.text import TextCompany
from waitless.decision.menu_parse import parse_menu

AGENT_LINES = [
    'Where would you like to start service? Please say My home, or My business.',
    'We can text you a link to finish your start service request in our web app. Would you like to use the web app?',
    'Before we continue by phone, may we send a text message with the web app link to the number you are calling from?',
    "All right, let's continue by phone. What is the service address?",
    'On what date should service start?',
    'Please enter your phone number, followed by the pound key.',
    'Your new service needs a special approval. Let me transfer you to a representative.',
]


def load(root):
    return load_scenario(root, 'lakeshore-energy', 'baseline'), load_company(root, 'lakeshore-energy')


def test_u112_route_1_menus_parse_all_keys(company_dir, skills_dir):
    scenario, _ = load(company_dir)
    rules = yaml.safe_load((skills_dir / 'navigate-ivr/references/ivr-rules.yaml').read_text())
    for name, keys in [('main_menu', ['8', '1', '2', '3', '9', '#']),
                       ('service_menu', ['1', '2', '3', '4', '*', '#'])]:
        assert [o.digits for o in parse_menu(scenario.steps[name].text, rules['press_patterns'])] == keys
    assert not any(word in scenario.steps['service_menu'].text.lower()
                   for word in ('social security', 'ssn', 'tax id'))


def test_u112_route_2_drive_reaches_confirmation(company_dir):
    scenario, company = load(company_dir)
    run = drive(TextCompany(scenario, company), menu_digits=['3', '4'], customer=company.customers[0])
    assert run.problem is None and run.stalled_on is None
    assert run.menu_presses == ['3', '4'] and run.interrupts == 1
    assert re.fullmatch(r'LE-\d{5}', run.confirmation or '')
    assert run.confirm_line.expect.outcome == 'confirmed'


def test_u112_route_3_star_and_pound(company_dir):
    scenario, company = load(company_dir)
    engine = ScenarioEngine(scenario, company)
    engine.start()
    assert engine.on_digits('#')[-1].step_id == 'main_menu'
    assert engine.on_digits('3')[-1].step_id == 'service_menu'
    assert engine.on_digits('#')[-1].step_id == 'main_menu'
    engine.on_digits('3')
    lines = engine.on_digits('*')
    assert [(line.step_id, line.kind) for line in lines] == [('transfer_hold', 'hold'), ('rep', 'speech')]
    assert engine.step_log()[-2:] == ['transfer_hold', 'rep']


def test_u112_route_4_agent_lines_in_order(company_dir):
    scenario, company = load(company_dir)
    assert [line.say for line in scenario.steps['agent'].text_script] == AGENT_LINES
    engine = ScenarioEngine(scenario, company)
    engine.start()
    engine.on_digits('3')
    lines = engine.on_digits('4')
    observed = [lines[-1].text]
    for answer in ['My home', 'No, continue by phone.', 'No texts.', 'The service address.', 'November 1.']:
        observed.extend(line.text for line in engine.on_speech(answer) if line.role == 'agent')
    observed.extend(line.text for line in engine.on_digits('3125550147#') if line.role == 'agent')
    assert observed == AGENT_LINES
