import pytest
import yaml

from tests.company._live_harness import company_settings
from tests.company._scenario_data import copy_company_root, edit_scenario_yaml
from waitless.company.app import create_company_app, run_company
from waitless.company.config import ScenarioError, load_company, load_scenario
from waitless.company import scripted
from waitless.company.engine import _live_scenario

FIELDS = ('holder_prompt_ms', 'holder_max_asks', 'resume_wait_ms')


def test_baseline_handoff_fields(company_dir):
    company = load_company(company_dir, 'lakeshore-energy')
    scenario = load_scenario(company_dir, company.company_id, 'baseline')
    assert scripted.scripted_handoff(scenario.steps['rep'], company) == scripted.ScriptedHandoff(3000, 4, 8000)
    assert scripted.scripted_handoff(scenario.steps['agent'], company) is None


def test_holder_reply_for_check_live(company_dir):
    data = yaml.safe_load((company_dir / 'lakeshore-energy/scenarios/baseline.yaml').read_text())
    reply = data['steps']['rep']['scripted']['holder_reply']
    assert isinstance(reply, dict) and isinstance(reply['say'], str) and reply['say'].strip()
    assert reply['polly_voice'] == 'Salli'


@pytest.mark.parametrize('field', FIELDS)
@pytest.mark.parametrize('value', ['missing', 0, -1, '3000', True, 1.5])
def test_invalid_handoff_fields(company_dir, tmp_path, field, value):
    root = copy_company_root(company_dir, tmp_path)
    def edit(data):
        fields = data['steps']['rep']['scripted']
        fields.update(holder_prompt_ms=3000, holder_max_asks=4, resume_wait_ms=8000)
        if value == 'missing':
            fields.pop(field)
        else:
            fields[field] = value
    edit_scenario_yaml(root, 'baseline', edit)
    company = load_company(root, 'lakeshore-energy')
    with pytest.raises(ScenarioError, match=rf'rep:.*{field}'):
        scripted.check_scripted_steps(load_scenario(root, company.company_id, 'baseline'), company)


def test_holder_reply_is_not_validated(company_dir, tmp_path):
    root = copy_company_root(company_dir, tmp_path)
    edit_scenario_yaml(root, 'baseline', lambda d: d['steps']['rep']['scripted'].pop('holder_reply', None))
    company = load_company(root, 'lakeshore-energy')
    scripted.check_scripted_steps(load_scenario(root, company.company_id, 'baseline'), company)


def test_no_handoff_needs_no_fields(company_dir, tmp_path):
    root = copy_company_root(company_dir, tmp_path)
    def edit(data):
        rep = data['steps']['rep']
        rep['text_script'] = [line for line in rep['text_script']
                              if line.get('expect', {}).get('intent') != 'request_handoff'
                              and line.get('expect', {}).get('action') != 'handoff']
        rep['live_view']['text_script'] = [line for line in rep['live_view']['text_script']
                                         if line.get('expect', {}).get('action') != 'handoff']
        for field in FIELDS:
            rep['scripted'].pop(field, None)
    edit_scenario_yaml(root, 'baseline', edit)
    company = load_company(root, 'lakeshore-energy')
    scenario = _live_scenario(load_scenario(root, company.company_id, 'baseline'))
    scripted.check_scripted_steps(scenario, company)
    assert scripted.scripted_handoff(scenario.steps['rep'], company) is None


def test_action_handoff_requires_fields(company_dir, tmp_path):
    root = copy_company_root(company_dir, tmp_path)
    def edit(data):
        request = data['steps']['rep']['live_view']['text_script'][0]['expect']
        request['intent'] = 'open_answer'
        request['action'] = 'handoff'
        data['steps']['rep']['scripted'].pop('holder_max_asks', None)
    edit_scenario_yaml(root, 'baseline', edit)
    company = load_company(root, 'lakeshore-energy')
    with pytest.raises(ScenarioError, match='rep:.*holder_max_asks'):
        scripted.check_scripted_steps(_live_scenario(load_scenario(root, company.company_id, 'baseline')), company)


def test_invalid_startup_only_scripted(company_dir, tmp_path, mode_env, engine_config, monkeypatch, capsys):
    root = copy_company_root(company_dir, tmp_path)
    edit_scenario_yaml(root, 'baseline', lambda d: d['steps']['rep']['scripted'].pop('resume_wait_ms', None))
    settings = company_settings(mode_env('live-sim', company=True), engine_config, root, tmp_path)
    with pytest.raises(ScenarioError, match='rep:.*resume_wait_ms'):
        create_company_app(settings, agent_mode='scripted')
    assert create_company_app(settings, agent_mode='sonic').state.carrier.agent_mode == 'sonic'
    import uvicorn
    monkeypatch.setattr(uvicorn, 'run', lambda *a, **kw: pytest.fail('invalid startup ran uvicorn'))
    monkeypatch.setenv(scripted.AGENT_MODE_ENV, 'scripted')
    assert run_company(settings) == 2
    assert 'resume_wait_ms' in capsys.readouterr().err
