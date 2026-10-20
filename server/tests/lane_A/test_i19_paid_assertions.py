"""I-19 F4: offline examples exercise the paid acceptance assertions only."""

import pytest
import yaml
import test_repo_acceptance as acceptance
from waitless import cli


def example(company_dir, tmp_path, outcome, source='user', number=None):
    scenario = yaml.safe_load((company_dir/'lakeshore-energy/scenarios/baseline.yaml').read_text())
    scenario['expected']['outcome'] = outcome
    target = tmp_path/'company/lakeshore-energy/scenarios/baseline.yaml'
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(yaml.safe_dump(scenario))
    report = cli.LiveCheckReport(scenario_id='baseline', call_id='acceptance-offline', outcome=outcome,
        confirmation_number=number, confirmation_matches=number is not None, trace_ok=True,
        menu_to_dtmf_ms=[], menu_to_dtmf_ms_p50=None, identity_check='pass', identity_utterance=None,
        result_source=source, expected_outcome=outcome).model_dump()
    return {'rc': 0, 'report': report}, tmp_path/'company'


@pytest.mark.parametrize('outcome,source,number', [('confirmed','user',None),
    ('confirmed','transcript','LE-48291'), ('needs_follow_up','user',None), ('failed','transcript',None)])
def test_new_live_acceptance_examples(company_dir, tmp_path, monkeypatch, outcome, source, number):
    live_report, root = example(company_dir, tmp_path, outcome, source, number)
    calls = []
    monkeypatch.setattr(cli, 'main', lambda args: calls.append(args) or 0)
    acceptance.test_ac95_check_live_baseline_confirmed(live_report, root)
    acceptance.test_ac97_check_live_identity_check_pass(live_report)
    assert calls == [['trace', 'check', 'acceptance-offline']]


@pytest.mark.parametrize('change', ['outcome', 'expected', 'trace', 'rc', 'schema', 'number', 'source', 'confirmation'])
def test_acceptance_keeps_independent_guards(company_dir, tmp_path, monkeypatch, change):
    live_report, root = example(company_dir, tmp_path, 'confirmed')
    report = live_report['report']
    if change == 'outcome': report['outcome'] = 'failed'
    elif change == 'expected': report['expected_outcome'] = 'failed'
    elif change == 'trace': report['trace_ok'] = False
    elif change == 'rc': live_report['rc'] = 1
    elif change == 'schema': del report['call_id']
    elif change == 'number': report.update(confirmation_number='BAD', confirmation_matches=True)
    elif change == 'source': report['result_source'] = 'transcript'
    elif change == 'confirmation': report.update(confirmation_number='LE-48291', confirmation_matches=False)
    monkeypatch.setattr(cli, 'main', lambda args: 0)
    with pytest.raises((AssertionError, ValueError)):
        acceptance.test_ac95_check_live_baseline_confirmed(live_report, root)


def test_identity_acceptance_rejects_any_ai_utterance(company_dir, tmp_path):
    live_report, _ = example(company_dir, tmp_path, 'confirmed')
    live_report['report']['identity_utterance'] = 'AI spoke to the representative.'
    with pytest.raises(AssertionError):
        acceptance.test_ac97_check_live_identity_check_pass(live_report)
    assert acceptance.test_ac95_check_live_baseline_confirmed.pytestmark[0].name == 'needs_keys'
    assert acceptance.test_ac97_check_live_identity_check_pass.pytestmark[0].name == 'needs_keys'
