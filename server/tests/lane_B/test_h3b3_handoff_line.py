import asyncio
import json
import logging
import re
import time

import pytest
from tests.company._scenario_data import edit_scenario_yaml
from tests.company.test_audio_carrier_line import send_keys, wait_prompt_finished
from tests.company._live_harness import wait_until
from tests.lane_B.test_sc3_scripted_carrier import (
    rig as sc_rig, connect_rig, reach_keypad, speak, wait_line,
)

from tests.lane_B.test_u112_deposit_window import OK, DEPOSIT, AGREE, CLOSE
from tests.company.test_audio_carrier_line import send_keys

QUESTION = 'Please say your date of birth, or enter it on your keypad as eight digits, followed by the pound key.'


@pytest.fixture
def rig(sc_rig):
    edit_scenario_yaml(sc_rig[0].company_dir, 'baseline', lambda d: d['steps']['rep']['scripted'].update(
        holder_prompt_ms=600, holder_max_asks=2, resume_wait_ms=1500))
    return sc_rig


async def reach_handoff(conn, rig):
    _, _, polly, times, _, company, scenario = rig
    await reach_keypad(conn, rig)
    await send_keys(conn, re.sub(r"\D", "", company.customers[0].phone_number) + '#')
    await wait_line(conn, polly, times, scenario.steps['rep'].text_script[0].say)
    await speak(conn, 200)
    await wait_line(conn, polly, times, QUESTION)


async def finish_call(conn, call, rig, confirmed=True):
    _, _, polly, times, _, _, scenario = rig
    rep = scenario.steps['rep'].text_script
    if not confirmed:
        await wait_line(conn, polly, times, CLOSE)
        assert call._engine.confirmation_number() is None
        await wait_until(lambda: conn.closed_at is not None, 6, 'stop')
        assert call._engine.ended and conn.events()[-1] == 'stop'
        return
    await wait_until(lambda: call._engine.confirmation_number() is not None, 6, 'confirmation')
    confirmation = rep[1].say.replace('{confirmation_number}', call._engine.confirmation_number())
    await wait_line(conn, polly, times, confirmation)
    await wait_until(lambda: conn.closed_at is not None, 6, 'stop')
    assert call._engine.ended
    assert call._engine.step_log() == ['greeting', 'main_menu', 'service_menu', 'hold', 'agent', 'rep']
    assert conn.events()[-1] == 'stop'


def stats(rig, call, frames):
    rows = [json.loads(line) for line in rig[0].company_stats_path.read_text().splitlines()]
    assert len(rows) == 1
    assert rows[0]['call_sid'] == call.call_sid
    assert rows[0]['nonsilent_inbound_frames_during_handoff'] == frames
    assert rows[0]['rep_first_audio_at'] <= rows[0]['rep_last_audio_at']
    assert rows[0]['rep_first_line_end_at'] is not None
    assert rows[0]['nonsilent_inbound_frames_during_rep'] == len(rows[0]['nonsilent_inbound_timestamps_during_rep'])
    assert rig[4] == []


async def test_holder_only_count_and_question_playback(rig, caplog):
    caplog.set_level(logging.INFO)
    _, _, polly, _, _, company, scenario = rig
    async with connect_rig(rig) as (_, call, conn):
        await reach_handoff(conn, rig)
        assert call._scripted_turns.phase == 'holder_answer'
        await speak(conn, 200)  # Holder: ten frames counted.
        await wait_line(conn, polly, rig[3], DEPOSIT)
        assert call._scripted_turns.in_handoff_window
        await send_keys(conn, '1')
        await wait_line(conn, polly, rig[3], AGREE)
        assert call._scripted_turns.phase == 'resume_wait'
        await speak(conn, 200)  # Resume: excluded from AC-98.
        await finish_call(conn, call, rig)
        rep = scenario.steps['rep'].text_script
        expected = [line.say for line in scenario.steps['agent'].text_script]
        expected += [rep[0].say, QUESTION, OK, DEPOSIT, AGREE,
                     rep[1].say.replace('{confirmation_number}', call._engine.confirmation_number())]
        speech = [c for c in polly.calls if c['Text'] in expected]
        assert [c['Text'] for c in speech] == expected
        assert [c['VoiceId'] for c in speech] == [company.voices.ivr_polly] * 7 + ['Matthew'] * 6
    stats(rig, call, 10)
    messages = [r.getMessage() for r in caplog.records]
    assert messages.count('scripted handoff ask n=1 step=rep') == 2
    assert messages.count('scripted holder answered') == 1
    assert messages.count('scripted handoff window closed step=rep reason=resume') == 1


async def test_no_answer_replays_cached_question_and_idles(rig, caplog):
    caplog.set_level(logging.INFO)
    _, _, polly, _, _, _, _ = rig
    async with connect_rig(rig) as (_, call, conn):
        await reach_handoff(conn, rig)
        first_finished = time.monotonic()
        await wait_prompt_finished(conn, quiet_s=0.2, after=first_finished)
        assert call._scripted_turns.phase == 'holder_answer'
        assert sum(c['Text'] == QUESTION for c in polly.calls) == 1
        await wait_until(lambda: call._scripted_turns.phase == 'resume_wait', 6, 'no answer limit')
        await finish_call(conn, call, rig, confirmed=False)
    stats(rig, call, 0)
    messages = [r.getMessage() for r in caplog.records]
    assert messages.count('scripted handoff ask n=1 step=rep') == 1
    assert messages.count('scripted handoff ask n=2 step=rep') == 1
    assert messages.count('scripted holder no answer') == 1
    assert messages.count('scripted handoff window closed step=rep reason=idle') == 1
    # The live opening line has action=handoff without an intent, followed by two private asks.
    assert sum('scripted line step=rep intent=None voice=Matthew' == m for m in messages) == 3
