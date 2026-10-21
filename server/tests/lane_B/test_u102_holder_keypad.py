import asyncio
import base64
import json
import logging
import re

import pytest

from tests.company._scenario_data import copy_company_root, edit_scenario_yaml
from tests.company._signals import dtmf_tone, silence, ulaw_encode
from tests.lane_B.test_h3b2_handoff_window import open_window, speak
from tests.lane_B.test_h3b3_handoff_line import reach_handoff, rig  # noqa: F401
from tests.lane_B.test_sc2_scripted_turns import setup_turns, agent_batch
from tests.lane_B.test_sc3_scripted_carrier import connect_rig, wait_line
from tests.lane_B.test_sc3_scripted_carrier import rig as sc_rig  # noqa: F401
from waitless.company.app import create_company_app
from waitless.company.config import ScenarioError
from waitless.company.engine import ScenarioEngine
from tests.lane_B.test_u112_deposit_window import DEPOSIT, AGREE, play
from waitless.company.dtmf_detect import GoertzelDetector
from waitless.company.scripted import check_scripted_steps

QUESTION = ('Please say your date of birth, or enter it on your keypad as eight digits, '
            'followed by the pound key.')
OK = 'Thank you, I have that.'
KEYS = '01021990'


def joined_engine(root, config):
    _, _, company, scenario = setup_turns(root, config)
    engine = ScenarioEngine(scenario, company)
    agent_batch(engine)
    engine.on_speech('reply')
    engine.on_speech('reply')
    engine.on_digits(re.sub(r'\D', '', company.customers[0].phone_number) + '#')
    engine.on_speech('reply')
    assert engine.on_digits(KEYS + '#') == []  # Before joining, no entry is accepted.
    questions = engine.on_handoff(True)
    return engine, questions


def enter_keypad(engine):
    for key in KEYS[:-1]:
        assert engine.on_digits(key) == []
    result = engine.on_digits(KEYS[-1])
    line, deposit = result
    assert deposit.text == DEPOSIT
    assert line.text == OK and line.role == 'rep' and not any(c.isdigit() for c in line.text)
    return result


def test_u102_engine_keypad_in_window_completes_with_ok_line(company_dir, engine_config):
    engine, questions = joined_engine(company_dir, engine_config)
    assert len(questions) == 1 and questions[0].text == QUESTION
    assert engine.on_digits('*') == []
    enter_keypad(engine)
    assert engine.on_digits('#') == []
    assert [line.text for line in engine.on_digits(KEYS + '#')] == [DEPOSIT]
    assert [line.text for line in engine.on_digits('1')] == [AGREE]
    assert engine.on_handoff(False)[0].expect.outcome == 'confirmed'
    assert engine.on_digits(KEYS) == []


def test_u102_engine_wrong_length_reasks(company_dir, engine_config):
    engine, _ = joined_engine(company_dir, engine_config)
    assert engine.on_digits(KEYS[:5]) == []
    question, = engine.on_digits('#')
    assert question.text == QUESTION and question.role == 'rep'
    enter_keypad(engine)


def test_u102_engine_without_keypad_digits_is_unchanged(company_dir, tmp_path, engine_config):
    root = copy_company_root(company_dir, tmp_path)
    def edit(data):
        action = next(a for a in data['steps']['rep']['actions'] if a['do'] == 'require_account_holder')
        action.pop('keypad_digits', None)
        action.pop('keypad_ok_line', None)
    edit_scenario_yaml(root, 'baseline', edit)
    engine, _ = joined_engine(root, engine_config)
    for key in KEYS + '#*':
        assert engine.on_digits(key) == []
    assert engine.on_handoff(False)[0].expect.outcome == 'confirmed'


def test_u102_check_scripted_steps_validates_extension(company_dir, engine_config):
    _, _, company, scenario = setup_turns(company_dir, engine_config)
    check_scripted_steps(scenario, company)
    for value in (0, -1, 2.5, '8', True, None, 8):
        changed = scenario.model_copy(deep=True)
        action = next(a for a in changed.steps['rep'].actions if a.do == 'require_account_holder')
        action.keypad_digits = value
        action.model_extra.pop('keypad_ok_line', None)
        with pytest.raises(ScenarioError):
            check_scripted_steps(changed, company)


def test_u102_scripted_turns_tones_are_not_speech(company_dir, engine_config):
    engine, turns, window = open_window(company_dir, engine_config)
    question, = turns.poll(window + 3000)
    turns.line_started(question)
    now = window + 3100
    turns.line_finished(question, now)
    detector = GoertzelDetector(sample_rate=8000)
    # A candidate's first window and a held key must both be excluded from speech.
    for pcm in (dtmf_tone('0', 2000), silence(1600)):
        for offset in range(0, len(pcm), 320):
            now += 20
            frame = pcm[offset:offset + 320]
            detector.feed(frame)
            if detector.tone_present:
                turns.feed_keypad_tone(now)
            else:
                turns.feed(frame, now)
            assert turns.poll(now) == []
            assert turns.phase == 'holder_answer'
    # Wrong length reasks inside the same window and restarts the answer timer.
    engine.on_digits(KEYS[:5])
    repeated = engine.on_digits('#')
    turns.holder_keypad_lines(repeated)
    turns.line_started(repeated[0])
    assert turns.in_handoff_window
    turns.line_finished(repeated[0], now)
    assert turns.phase == 'holder_answer'
    lines = enter_keypad(engine)
    turns.holder_keypad_lines(lines)
    turns.line_started(lines[0])
    assert turns.in_handoff_window
    assert turns.poll(now + 100000) == []
    now = play(turns, lines, now + 100100)
    assert turns.phase == 'holder_answer' and turns.in_handoff_window
    chosen = engine.on_digits('1')
    turns.holder_keypad_lines(chosen)
    now = play(turns, chosen, now)
    assert turns.phase == 'resume_wait' and turns.in_handoff_window
    last = speak(turns, now + 100, 200)
    result, = turns.poll(last + 1500)
    assert result.expect.outcome == 'confirmed'


async def test_u102_scripted_carrier_end_to_end(rig, caplog):
    caplog.set_level(logging.DEBUG)
    _, _, polly, times, _, _, scenario = rig
    async with connect_rig(rig) as (_, call, conn):
        await reach_handoff(conn, rig)
        for i, key in enumerate(KEYS + '#'):
            pcm = dtmf_tone(key, 100) + silence(100 + i % 3 * 100) + silence(20)
            ulaw = ulaw_encode(pcm)
            for offset in range(0, len(ulaw), 160):
                frame = base64.b64encode(ulaw[offset:offset + 160]).decode('ascii')
                await conn.send({'event': 'media', 'streamSid': conn.stream_sid,
                                 'media': {'payload': frame}})
                await asyncio.sleep(0.02)
        await wait_line(conn, polly, times, OK)
        assert sum(c['Text'] == OK for c in polly.calls) == 1
        await wait_line(conn, polly, times, DEPOSIT)
        from tests.company.test_audio_carrier_line import send_keys
        await send_keys(conn, '1')
        await wait_line(conn, polly, times, AGREE)
        assert call._scripted_turns.phase == 'resume_wait'
        assert call._scripted_turns.in_handoff_window
        assert call._nonsilent_inbound_frames == 0
        from tests.lane_B.test_sc3_scripted_carrier import speak as line_speak
        await line_speak(conn, 200)
        from tests.company._live_harness import wait_until
        await wait_until(lambda: call._engine.confirmation_number() is not None, 6, 'confirmation')
        confirmation = scenario.steps['rep'].text_script[1].say.replace(
            '{confirmation_number}', call._engine.confirmation_number())
        await wait_line(conn, polly, times, confirmation)
        assert not call._scripted_turns.in_handoff_window
    assert 'scripted holder keypad entry complete' in caplog.text
    assert KEYS not in caplog.text
    assert KEYS not in json.dumps(polly.calls)


def test_u102_bank_and_other_scenarios_still_load(rig, company_dir):
    settings = rig[0].model_copy(update={'company_dir': company_dir})
    app = create_company_app(settings, agent_mode='scripted')
    assert app.state.carrier.agent_mode == 'scripted'
