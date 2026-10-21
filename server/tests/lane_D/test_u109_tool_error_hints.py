"""U109: failed tools forward editable, skill-owned hints unchanged."""
from pathlib import Path
import shutil

import yaml

from lane_D.agent_support import *


ASK = {'question': 'Which date?', 'options': [{'label': 'Monday'}, {'label': 'Tuesday'}]}


def hint_file(skills_dir):
    return Path(skills_dir) / 'phone-agent/references/tool-errors.yaml'


def hint_for(skills_dir, code):
    return yaml.safe_load(hint_file(skills_dir).read_text(encoding='utf-8'))['errors'][code]


def copy_skills(h, tmp_path):
    target = tmp_path / 'skills'
    shutil.copytree(h.settings.skills_dir, target, ignore=shutil.ignore_patterns('hidden'))
    h.settings = h.settings.model_copy(update={'skills_dir': target})
    h.deps.settings = h.settings
    return hint_file(target)


def test_u109_hints_file_shape(skills_dir):
    path = hint_file(skills_dir)
    assert path.is_file()
    data = yaml.safe_load(path.read_text(encoding='utf-8'))
    assert isinstance(data, dict) and set(data) == {'errors'}
    errors = data['errors']
    assert isinstance(errors, dict)
    assert {'choice_already_open', 'interrupt_budget_exhausted', 'handoff_already_active'} <= errors.keys()
    assert all(isinstance(code, str) and code.strip()
        and isinstance(hint, str) and hint.strip() for code, hint in errors.items())
    assert agent_module().load_tool_error_hints(skills_dir) == errors


async def test_u109_choice_already_open_carries_hint(agent_call):
    h = agent_call
    await h.boot()
    await h.tool('ask_user', ASK)
    result = await h.tool('ask_user', ASK)
    assert result == {'ok': False, 'error': 'choice_already_open',
        'hint': hint_for(h.settings.skills_dir, 'choice_already_open')}
    assert h.events('agent_tool_result')[-1].action['result'] == result
    assert len(h.events('pending_choice_created')) == 1
    h.assert_trace_contract()


async def test_u109_budget_error_carries_hint_and_keeps_counts(agent_call):
    h = agent_call
    h.spec.compiled.policy['global']['interrupt_budget'] = 1
    await h.boot()
    await h.tool('ask_user', ASK)
    choice = h.call.status().pending_choice
    await h.call.answer(choice.choice_id, choice.options[0].id)
    result = await h.tool('ask_user', ASK)
    assert result == {'ok': False, 'error': 'interrupt_budget_exhausted', 'used': 1, 'budget': 1,
        'hint': hint_for(h.settings.skills_dir, 'interrupt_budget_exhausted')}
    assert h.call.status().interrupts_used == 1
    assert h.call.status().pending_choice is None
    assert h.events('agent_tool_result')[-1].action['result'] == result


async def test_u109_unknown_code_gets_no_hint(agent_call, tmp_path):
    h = agent_call
    path = copy_skills(h, tmp_path)
    data = yaml.safe_load(path.read_text(encoding='utf-8'))
    del data['errors']['choice_already_open']
    path.write_text(yaml.safe_dump(data), encoding='utf-8')
    await h.boot()
    assert 'hint' not in await h.tool('update_status', {'phase': 'hold', 'status_line': 'Waiting'})
    assert 'hint' not in await h.tool('ask_user', ASK)
    result = await h.tool('ask_user', ASK)
    assert result == {'ok': False, 'error': 'choice_already_open'}
    assert 'hint' not in h.events('agent_tool_result')[-1].action['result']


async def test_u109_bad_file_fails_the_call_setup(agent_call, tmp_path):
    h = agent_call
    path = copy_skills(h, tmp_path)
    load = agent_module().load_tool_error_hints
    path.unlink()
    with pytest.raises(FileNotFoundError):
        load(h.settings.skills_dir)
    invalid_files = [[], None, {}, {'errors': []}, {'errors': {'choice_already_open': ''}},
        {'errors': {'choice_already_open': '   '}}, {'errors': {'choice_already_open': 1}},
        {'errors': {1: 'Guidance'}}, {'errors': {'': 'Guidance'}},
        {'errors': {}, 'extra': {}}]
    for data in invalid_files:
        path.write_text(yaml.safe_dump(data), encoding='utf-8')
        with pytest.raises(ValueError):
            load(h.settings.skills_dir)
    path.write_text('errors: [', encoding='utf-8')
    with pytest.raises(ValueError):
        load(h.settings.skills_dir)
    path.write_text(yaml.safe_dump({'errors': {'choice_already_open': ''}}), encoding='utf-8')
    with pytest.raises(ValueError):
        await h.boot()
    assert not h.sonic_builds


async def test_u109_hint_text_edits_flow_through(agent_call, tmp_path):
    h = agent_call
    path = copy_skills(h, tmp_path)
    data = yaml.safe_load(path.read_text(encoding='utf-8'))
    edited = '  Edited skill guidance.\nKeep this spacing.  '
    data['errors']['choice_already_open'] = edited
    path.write_text(yaml.safe_dump(data), encoding='utf-8')
    await h.boot()
    # Configuration is read once at setup, so later edits affect the next call.
    data['errors']['choice_already_open'] = 'Changed after setup'
    path.write_text(yaml.safe_dump(data), encoding='utf-8')
    await h.tool('ask_user', ASK)
    result = await h.tool('ask_user', ASK)
    assert result == {'ok': False, 'error': 'choice_already_open', 'hint': edited}
    assert h.events('agent_tool_result')[-1].action['result'] == result
