import httpx
import pytest
import uvicorn

from tests.company._live_harness import company_settings, dial_body, running_company
from tests.company._scenario_data import copy_company_root, edit_scenario_yaml
from waitless.company.app import create_company_app, run_company
from waitless.company.carrier import CarrierState
from waitless.company.config import ScenarioError, load_company, load_scenario
from waitless.company.scripted import (
    AGENT_MODE_ENV, ScriptedStep, agent_mode_from_env, check_scripted_steps, scripted_step,
)


def test_mode_values():
    for value in ({}, {AGENT_MODE_ENV: ''}, {AGENT_MODE_ENV: '  '}):
        assert agent_mode_from_env(value) == 'sonic'
    assert agent_mode_from_env({AGENT_MODE_ENV: ' scripted '}) == 'scripted'
    for mode in ('bogus', 'Scripted'):
        with pytest.raises(ScenarioError, match=AGENT_MODE_ENV):
            agent_mode_from_env({AGENT_MODE_ENV: mode})


def test_run_company_modes(monkeypatch, capsys, mode_env, engine_config, company_dir, tmp_path):
    calls = []
    monkeypatch.setattr(uvicorn, 'run', lambda app, **kw: calls.append(app))
    settings = company_settings(mode_env('live-sim', company=True), engine_config, company_dir, tmp_path)
    monkeypatch.setenv(AGENT_MODE_ENV, 'bogus')
    assert run_company(settings) == 2
    assert AGENT_MODE_ENV in capsys.readouterr().err
    assert calls == []
    monkeypatch.setenv(AGENT_MODE_ENV, 'scripted')
    assert run_company(settings) == 0
    assert len(calls) == 1 and calls[-1].state.carrier.agent_mode == 'scripted'
    monkeypatch.delenv(AGENT_MODE_ENV)
    assert run_company(settings) == 0
    assert calls[-1].state.carrier.agent_mode == 'sonic'


def test_baseline_fields(company_dir):
    company = load_company(company_dir, 'lakeshore-energy')
    scenario = load_scenario(company_dir, company.company_id, 'baseline')
    check_scripted_steps(scenario, company)
    assert scripted_step(scenario.steps['agent'], company) == ScriptedStep(1500, company.voices.ivr_polly)
    assert scripted_step(scenario.steps['rep'], company) == ScriptedStep(1500, 'Matthew')


def test_invalid_step_fields(company_dir, tmp_path):
    root = copy_company_root(company_dir, tmp_path)
    company = load_company(root, 'lakeshore-energy')
    cases = [('agent', 'missing'), ('agent', None), ('agent', []), ('agent', {})]
    cases += [('agent', {'reply_end_silence_ms': v}) for v in (0, -1, '1500', True, 1.5)]
    cases += [('rep', {'reply_end_silence_ms': 1500, 'polly_voice': v}) for v in ('', ' ', 5, None)]
    for name, value in cases:
        def edit(data):
            data['steps']['agent']['scripted'] = {'reply_end_silence_ms': 1500}
            data['steps']['rep']['scripted'] = {'reply_end_silence_ms': 1500}
            if value == 'missing':
                data['steps'][name].pop('scripted')
            else:
                data['steps'][name]['scripted'] = value
        edit_scenario_yaml(root, 'baseline', edit)
        with pytest.raises(ScenarioError, match=name):
            check_scripted_steps(load_scenario(root, company.company_id, 'baseline'), company)


def test_startup_validation(company_dir, tmp_path, mode_env, engine_config):
    root = copy_company_root(company_dir, tmp_path)
    edit_scenario_yaml(root, 'baseline', lambda d: d['steps']['agent'].pop('scripted', None))
    settings = company_settings(mode_env('live-sim', company=True), engine_config, root, tmp_path)
    with pytest.raises(ScenarioError, match='agent'):
        create_company_app(settings, agent_mode='scripted')
    assert create_company_app(settings, agent_mode='sonic').state.carrier.agent_mode == 'sonic'
    with pytest.raises(ScenarioError, match=AGENT_MODE_ENV):
        create_company_app(settings, agent_mode='bogus')


async def test_dial_revalidates(company_dir, tmp_path, mode_env, engine_config):
    root = copy_company_root(company_dir, tmp_path)
    env = mode_env('live-sim', company=True)
    settings = company_settings(env, engine_config, root, tmp_path)
    app = create_company_app(settings, agent_mode='scripted')
    async with running_company(app, env['WAITLESS_COMPANY_HOST']) as base:
        edit_scenario_yaml(root, 'baseline', lambda d: d['steps']['rep'].pop('scripted'))
        async with httpx.AsyncClient(base_url=base, trust_env=False) as http:
            response = await http.post('/carrier/calls', json=dial_body(
                call_id='call-config', task_id='task-config', to_number=load_company(root, 'lakeshore-energy').line_number,
                stream_url=env['WAITLESS_LINE_WS_URL'], stream_token='test-config-token'))
        assert response.status_code == 500
        assert response.json()['code'] == 'invalid_configuration'
        assert app.state.carrier._calls == {}


def test_default_state(company_dir):
    assert CarrierState([load_company(company_dir, 'lakeshore-energy')]).agent_mode == 'sonic'
