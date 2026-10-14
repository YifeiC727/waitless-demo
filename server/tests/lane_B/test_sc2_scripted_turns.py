import re

import pytest
from fakes.polly import tone_pcm16
from tests.company._scenario_data import copy_company_root, edit_scenario_yaml
from waitless.company.config import load_company, load_scenario
from waitless.company.engine import ScenarioEngine
from waitless.company.scripted import ReplyWaiter, ScriptedTurns
from waitless.models import CompanyLine, EvalExpect

VOICE = tone_pcm16(20, 8000)
SILENCE = bytes(320)


def speak(turns, t0, ms):
    for now in range(t0, t0 + ms, 20):
        assert turns.feed(VOICE, now)
    return now


def setup_turns(root, config):
    company = load_company(root, 'lakeshore-energy')
    scenario = load_scenario(root, company.company_id, 'baseline')
    engine = ScenarioEngine(scenario, company)
    turns = ScriptedTurns(engine=engine, scenario=scenario, company=company, engine_config=config)
    return engine, turns, company, scenario


def agent_batch(engine):
    engine.start()
    engine.on_digits('1')
    return engine.on_digits('1')


def test_waiter_boundaries(engine_config):
    waiter = ReplyWaiter(threshold_dbfs=engine_config.vad.threshold_dbfs)
    waiter.arm(end_silence_ms=1500, now_ms=0)
    assert waiter.armed
    for now in range(100, 401, 20):
        assert waiter.feed(VOICE, now)
    assert not waiter.due(1899)
    assert waiter.due(1900)
    waiter.disarm()
    assert not waiter.armed and not waiter.due(10000)
    waiter.feed(VOICE, 10000)
    waiter.arm(end_silence_ms=1500, now_ms=10001)
    assert not waiter.feed(SILENCE, 10002)
    assert not waiter.due(100000)
    # The configured threshold, strict comparison, and fresh arm govern energy.
    high = ReplyWaiter(threshold_dbfs=0)
    high.arm(end_silence_ms=1, now_ms=0)
    assert not high.feed(VOICE, 1) and not high.due(10000)
    waiter.feed(VOICE, 100100)
    waiter.arm(end_silence_ms=300, now_ms=100200)
    assert not waiter.due(200000)


def test_baseline_whole_call(company_dir, engine_config):
    engine, turns, company, _ = setup_turns(company_dir, engine_config)
    hold, first = agent_batch(engine)
    observed = []
    turns.line_finished(hold, 900)
    assert turns.phase == 'idle'
    turns.line_finished(first, 1000)
    assert turns.phase == 'reply'
    last = speak(turns, 1100, 200)
    assert turns.poll(last + 1499) == []
    second, = turns.poll(last + 1500)
    observed.append(second)
    turns.line_finished(second, 3000)
    last = speak(turns, 3100, 200)
    keypad, = turns.poll(last + 1500)
    observed.append(keypad)
    turns.line_finished(keypad, 5000)
    assert turns.phase == 'idle'
    last = speak(turns, 5100, 300)
    assert turns.poll(last + 10000) == []
    lines = engine.on_digits(company.customers[0].account_number + '#')
    assert len(lines) == 2
    transfer, identity = lines
    turns.lines_replaced()
    turns.line_finished(transfer, 16000)
    assert turns.phase == 'idle'
    turns.line_finished(identity, 17000)
    assert turns.phase == 'reply'
    last = speak(turns, 17100, 200)
    request, = turns.poll(last + 1500)
    observed.append(request)
    turns.line_finished(request, 19000)
    assert turns.phase == 'announce'
    last = speak(turns, 19100, 1000)
    assert turns.poll(last + 799) == [] and not turns.in_handoff_window
    assert turns.poll(last + 800) == [] and turns.in_handoff_window
    assert turns.phase == 'window'
    assert turns.poll(last + 10800) == [] and turns.in_handoff_window
    last = speak(turns, last + 11000, 1000)
    assert turns.poll(last + 1499) == []
    deposit, = turns.poll(last + 1500)
    observed.append(deposit)
    assert deposit.text.startswith('Verification is complete.')
    assert not turns.in_handoff_window and turns.phase == 'idle'
    turns.line_finished(deposit, last + 1600)
    last = speak(turns, last + 1700, 200)
    confirmation, = turns.poll(last + 1500)
    observed.append(confirmation)
    assert engine.confirmation_number() in confirmation.text
    assert re.search(r'LE-\d{5}', confirmation.text)
    turns.line_finished(confirmation, last + 1600)
    last = speak(turns, last + 1700, 200)
    hangup, = turns.poll(last + 1500)
    observed.append(hangup)
    assert hangup.kind == 'hangup' and engine.ended
    assert engine.step_log() == ['greeting', 'main_menu', 'service_menu', 'hold', 'agent', 'rep']
    assert all('Please confirm your date of birth.' not in line.text for line in observed)
    turns.line_finished(confirmation, last + 2000)
    assert turns.phase == 'idle'


def test_playback_and_replacement_disarm(company_dir, engine_config):
    engine, turns, _, _ = setup_turns(company_dir, engine_config)
    _, line = agent_batch(engine)
    turns.line_started(line)
    last = speak(turns, 0, 500)
    turns.line_finished(line, last + 20)
    assert not turns.feed(SILENCE, last + 100)
    assert turns.poll(last + 10000) == []
    turns.feed(VOICE, last + 11000)
    turns.lines_replaced()
    assert turns.phase == 'idle' and not turns.in_handoff_window
    assert turns.poll(last + 20000) == []
    # Non-response speech and IVR lines never arm.
    for candidate in [CompanyLine(step_id='agent', kind='speech', role='agent', text='No expectation'),
                      CompanyLine(step_id='agent', kind='speech', role='agent', text='Wait',
                                  expect=EvalExpect(intent='wait'))]:
        turns.line_finished(candidate, last + 21000)
        assert turns.phase == 'idle'


def test_configured_silence(company_dir, tmp_path, engine_config):
    root = copy_company_root(company_dir, tmp_path)
    edit_scenario_yaml(root, 'baseline', lambda d: d['steps']['agent']['scripted'].update(reply_end_silence_ms=300))
    config = engine_config.model_copy(update={'handoff': engine_config.handoff.model_copy(update={'announce_idle_ms': 500})})
    engine, turns, company, _ = setup_turns(root, config)
    _, line = agent_batch(engine)
    turns.line_finished(line, 0)
    last = speak(turns, 100, 200)
    assert turns.poll(last + 299) == []
    assert turns.poll(last + 300)[0].text.startswith('On what date')
    engine.on_speech('reply')
    identity = engine.on_digits(company.customers[0].account_number + '#')[-1]
    request = engine.on_speech('reply')[-1]
    turns.line_finished(request, 1000)
    last = speak(turns, 1100, 200)
    assert turns.poll(last + 499) == [] and not turns.in_handoff_window
    assert turns.poll(last + 500) == [] and turns.in_handoff_window
    turns.line_started(identity)
    assert not turns.in_handoff_window and turns.phase == 'idle'


def test_voice_for(company_dir, engine_config):
    engine, turns, company, _ = setup_turns(company_dir, engine_config)
    menu = engine.start()[-1]
    assert turns.voice_for(menu) == company.voices.ivr_polly
    engine.on_digits('1')
    agent = engine.on_digits('1')[-1]
    assert turns.voice_for(agent) == company.voices.ivr_polly
    engine.on_speech('reply')
    engine.on_speech('reply')
    rep = engine.on_digits(company.customers[0].account_number + '#')[-1]
    assert turns.voice_for(rep) == 'Matthew'
