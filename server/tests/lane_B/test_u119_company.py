"""v5.27 company boundaries: live holder, legacy text, long hold, wire evidence."""
import asyncio
import json
import re
import threading
import time

import pytest

from tests.company._scenario_data import drive
from tests.company._live_harness import wait_until
from tests.company.test_audio_carrier_line import send_keys
from tests.lane_B.test_sc2_scripted_turns import setup_turns, speak
from tests.lane_B.test_sc3_scripted_carrier import (
    rig, connect_rig, reach_keypad, speak as wire_speak, wait_line,
)
from tests.lane_B.test_u112_deposit_window import DOB, OK, DEPOSIT, AGREE, DECLINE, CLOSE
from waitless.company.config import list_scenarios, load_company, load_scenario
from waitless.company.text import TextCompany
from waitless.company.carrier import CarrierCall
from waitless.company.engine import _live_scenario
from tests.lane_B.test_sc2_scripted_turns import VOICE, SILENCE

FIRST = 'Thank you for holding. Am I speaking with the account holder?'
OLD_REQUEST = ('I need the account holder on this same call to verify their identity. '
               'Please bring them onto the line.')


def reach_rep(engine, scenario, company):
    lines = engine.start()
    for key in scenario.expected.menu_digits:
        lines = engine.on_digits(key)
    for _ in range(20):
        line = lines[-1]
        if line.role == 'rep':
            return line
        if (line.expect.intent or '').startswith('press_field:'):
            lines = engine.on_digits(re.sub(r'\D', '', company.customers[0].phone_number) + '#')
        else:
            lines = engine.on_speech('My home. Continue by phone.')
    pytest.fail('Representative not reached')


@pytest.mark.parametrize('key,outcome,feedback', [('1', 'confirmed', AGREE), ('2', 'needs_follow_up', DECLINE)])
def test_live_single_reply_opens_private_window_then_company_hangs_up(company_dir, engine_config, key, outcome, feedback):
    engine, turns, company, scenario = setup_turns(company_dir, engine_config)
    first = reach_rep(engine, scenario, company)
    assert first.text == FIRST
    assert first.expect.action == 'handoff' and first.expect.intent is None
    assert all('bring them onto the line' not in entry.say for entry in scenario.steps['rep'].text_script)
    turns.line_started(first)
    turns.line_finished(first, 1000)
    assert turns.poll(100000) == [] and not turns.in_handoff_window  # Silence alone cannot join.
    last = speak(turns, 100100, 200)  # The holder's single initial answer.
    opened = last + engine_config.handoff.announce_idle_ms
    assert turns.poll(opened - 1) == [] and not turns.in_handoff_window
    assert turns.poll(opened) == [] and turns.in_handoff_window
    delay = scenario.steps['rep'].scripted['holder_prompt_ms']
    assert turns.poll(opened + delay - 1) == []
    question, = turns.poll(opened + delay)
    assert question.text == DOB
    now = opened + delay + 100
    turns.line_started(question)
    turns.line_finished(question, now)
    assert engine.on_digits('0102199') == []
    lines = engine.on_digits('0#')
    turns.holder_keypad_lines(lines)
    assert [line.text for line in lines] == [OK, DEPOSIT]
    for line in lines:
        turns.line_started(line)
        now += 100
        turns.line_finished(line, now)
    lines = engine.on_digits(key)
    turns.holder_keypad_lines(lines)
    assert [line.text for line in lines] == [feedback]
    for line in lines:
        turns.line_started(line)
        now += 100
        turns.line_finished(line, now)
    # No Return, goodbye, or further speech from the caller is needed.
    lines = turns.poll(now + scenario.steps['rep'].scripted['resume_wait_ms'])
    assert lines[0].expect.outcome == outcome
    if outcome == 'confirmed':
        assert re.search(r'LE-\d{5}', lines[0].text)
        turns.line_started(lines[0])
        turns.line_finished(lines[0], now + 9000)
        lines += turns.poll(now + 99999)
    else:
        assert lines[0].text == CLOSE
    assert lines[-1].kind == 'hangup' and engine.ended


def test_text_keeps_identity_request_private_return_and_ideal_driver(company_dir):
    company = load_company(company_dir, 'lakeshore-energy')
    scenario = load_scenario(company_dir, company.company_id, 'baseline')
    text = TextCompany(scenario, company)
    first = reach_rep(text, scenario, company)
    assert first.text == 'Am I speaking with the account holder?'
    assert first.expect.intent == 'state_ai_identity'
    request, = text.on_speech('I am an AI assistant for the holder.')
    assert request.text == OLD_REQUEST and request.expect.intent == 'request_handoff'
    assert text.on_handoff(True)[0].text == DOB
    confirmed, = text.on_handoff(False)
    assert confirmed.confirmation_number and confirmed.expect.outcome == 'confirmed'
    assert text.on_speech('Thank you.')[0].kind == 'hangup'
    run = drive(TextCompany(scenario, company), menu_digits=scenario.expected.menu_digits, customer=company.customers[0])
    assert run.problem is None and run.stalled_on is None
    assert run.interrupts == 1 and run.confirmation
    assert _live_scenario(scenario).steps['rep'].text_script[0].say == FIRST
    # A selected live Scenario can also be reused by the text adapter.
    reused = TextCompany(_live_scenario(scenario), company)
    assert reach_rep(reused, scenario, company).expect.intent == 'state_ai_identity'


def test_bank_long_hold_loads_with_full_call_budget(company_dir):
    cid = 'bluefin-harbor-bank'
    assert 'long-hold' in list_scenarios(company_dir, cid)
    scenario = load_scenario(company_dir, cid, 'long-hold')
    baseline = load_scenario(company_dir, cid, 'baseline')
    assert scenario.steps['queue'].seconds == 1200
    assert scenario.max_call_seconds >= 1200 + baseline.max_call_seconds
    assert baseline.steps['queue'].seconds == 180
    assert scenario.steps['agent'] == baseline.steps['agent']
    assert scenario.steps['rep'] == baseline.steps['rep']


def test_rep_stats_use_receipt_time_and_never_infer_join_from_voice(engine_config):
    call = object.__new__(CarrierCall)
    call._engine_config = engine_config
    call._rep_first_audio_at = 10.0
    call._nonsilent_inbound_timestamps_during_rep = []
    call._record_rep_inbound(VOICE, 9.99)  # Arrived before rep; processed later.
    call._record_rep_inbound(SILENCE, 10.01)
    call._record_rep_inbound(VOICE, 10.02)
    call._record_rep_inbound(VOICE, 10.03)  # The first voice does not end evidence collection.
    assert call._nonsilent_inbound_timestamps_during_rep == [10.02, 10.03]


async def test_rep_stats_follow_actual_wire_audio_and_include_pre_holder_frames(rig, monkeypatch):
    settings, _, polly, times, _, company, scenario = rig
    first = scenario.steps['rep'].text_script[0].say
    started, release = threading.Event(), threading.Event()
    original = polly.synthesize_speech
    def slow(**kwargs):
        if kwargs['Text'] == first:
            started.set()
            assert release.wait(6)
        return original(**kwargs)
    monkeypatch.setattr(polly, 'synthesize_speech', slow)
    try:
        async with connect_rig(rig) as (_, call, conn):
            await reach_keypad(conn, rig)
            await send_keys(conn, re.sub(r'\D', '', company.customers[0].phone_number) + '#')
            await wait_until(started.is_set, 6, 'rep synthesis started')
            await wire_speak(conn, 60)  # Before rep playback; must be excluded.
            release.set()
            await wait_line(conn, polly, times, first)
            lower = time.monotonic()
            await wire_speak(conn, 100)  # No holder ready/join marker has occurred.
            await asyncio.sleep(0.05)
            upper = time.monotonic()
            assert not call._scripted_turns.in_handoff_window
            await call.hangup()
        row, = [json.loads(line) for line in settings.company_stats_path.read_text().splitlines()]
        assert row['nonsilent_inbound_frames_during_rep'] == 5
        stamps = row['nonsilent_inbound_timestamps_during_rep']
        assert len(stamps) == 5 and stamps == sorted(stamps)
        assert all(lower <= stamp <= upper for stamp in stamps)
        assert times[first] < row['rep_first_audio_at'] <= row['rep_last_audio_at']
        assert row['rep_last_audio_at'] < row['rep_first_line_end_at'] <= lower
        assert row['nonsilent_inbound_frames_during_handoff'] == 0
        assert set(row) == {'call_sid', 'nonsilent_inbound_frames_during_handoff',
                            'nonsilent_inbound_frames_during_rep', 'nonsilent_inbound_timestamps_during_rep',
                            'rep_first_audio_at', 'rep_last_audio_at', 'rep_first_line_end_at'}
    finally:
        release.set()
