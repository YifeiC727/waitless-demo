import asyncio
import base64
import logging
import threading

import pytest

from tests.lane_B.test_sc2_scripted_turns import setup_turns, speak
from waitless.company.config import ScenarioError
from waitless.company.scripted import check_scripted_steps

DOB = 'Please say your date of birth, or enter it on your keypad as eight digits, followed by the pound key.'
OK = 'Thank you, I have that.'
DEPOSIT = ('Your new service requires a refundable security deposit, billed once. '
           'To agree to the deposit, press 1. To decline, press 2.')
AGREE = 'Thank you, the deposit is noted.'
DECLINE = 'Understood, no deposit has been approved.'
CLOSE = ('Without the deposit we cannot start the service today. '
         'The account holder can call back to continue. Goodbye.')


def request(root, config):
    engine, turns, company, scenario = setup_turns(root, config)
    lines = engine.start()
    for key in scenario.expected.menu_digits:
        lines = engine.on_digits(key)
    for _ in range(20):
        line = lines[-1]
        if line.expect and (line.expect.intent == 'request_handoff' or line.expect.action == 'handoff'):
            return engine, turns, scenario, line
        if (line.expect.intent or '').startswith('press_field:'):
            lines = engine.on_digits('3125550147#')
        else:
            lines = engine.on_speech('My home. No, continue by phone.')
    pytest.fail('No holder request')


def window(root, config):
    engine, turns, scenario, line = request(root, config)
    turns.line_finished(line, 0)
    last = speak(turns, 100, 200)
    opened = last + config.handoff.announce_idle_ms
    assert turns.poll(opened) == [] and turns.in_handoff_window
    now = opened + 3000
    questions = turns.poll(now)
    assert [line.text for line in questions] == [DOB]
    return engine, turns, scenario, questions, now


def play(turns, lines, now):
    for line in lines:
        turns.line_started(line)
        assert turns.in_handoff_window
        now += 100
        turns.line_finished(line, now)
        assert turns.in_handoff_window
    return now


def keypad(engine, turns, keys):
    lines = engine.on_digits(keys)
    turns.holder_keypad_lines(lines)
    return lines


def deposit(root, config):
    engine, turns, scenario, lines, now = window(root, config)
    now = play(turns, lines, now)
    lines = keypad(engine, turns, '01021990#')
    assert [line.text for line in lines] == [OK, DEPOSIT]
    now = play(turns, lines, now)
    return engine, turns, scenario, now


def result(engine, lines, outcome):
    assert any(line.expect and line.expect.outcome == outcome for line in lines)
    if outcome == 'confirmed':
        assert engine.confirmation_number().startswith('LE-')
    else:
        assert engine.confirmation_number() is None
        assert all(line.confirmation_number is None for line in lines)
        assert lines[0].text == CLOSE


def test_u112_win_1_dob_first_then_deposit(company_dir, engine_config):
    for method in ('speech', 'keys'):
        engine, turns, _, lines, now = window(company_dir, engine_config)
        now = play(turns, lines, now)
        assert turns.poll(now + 1499) == []
        if method == 'keys':
            assert keypad(engine, turns, '0102199') == []
            lines = keypad(engine, turns, '0#')
        else:
            last = speak(turns, now + 20, 200)
            assert turns.poll(last + 1499) == []
            lines = turns.poll(last + 1500)
        assert [line.text for line in lines] == [OK, DEPOSIT]
        play(turns, lines, now + 2000)
        assert turns.in_handoff_window and turns.phase == 'holder_answer'


def test_u112_win_2_press_1_confirms(company_dir, engine_config):
    engine, turns, _, now = deposit(company_dir, engine_config)
    lines = keypad(engine, turns, '1')
    assert [line.text for line in lines] == [AGREE]
    now = play(turns, lines, now)
    last = speak(turns, now + 20, 200)
    result(engine, turns.poll(last + 1500), 'confirmed')
    assert not turns.in_handoff_window


def test_u112_win_3_press_2_follow_up(company_dir, engine_config):
    engine, turns, _, now = deposit(company_dir, engine_config)
    lines = keypad(engine, turns, '2')
    assert [line.text for line in lines] == [DECLINE]
    now = play(turns, lines, now)
    result(engine, turns.poll(now + 8000), 'needs_follow_up')
    assert engine.ended and not turns.in_handoff_window


async def test_u112_win_4_unanswered_or_early_return(company_dir, engine_config, audio_rig, monkeypatch):
    for how in ('unanswered', 'before_question', 'early_return', 'hangup', 'dob_only'):
        if how == 'before_question':
            engine, turns, _, line = request(company_dir, engine_config)
            turns.line_finished(line, 0)
            last = speak(turns, 100, 200)
            turns.poll(last + engine_config.handoff.announce_idle_ms)
            assert turns.in_handoff_window
        elif how in ('early_return', 'hangup'):
            engine, turns, _, lines, now = window(company_dir, engine_config)
            play(turns, lines, now)
        else:
            engine, turns, scenario, now = deposit(company_dir, engine_config)
        if how == 'unanswered':
            for _ in range(scenario.steps['rep'].scripted['holder_max_asks'] - 1):
                lines = turns.poll(now + 3000)
                assert [line.text for line in lines] == [DEPOSIT]
                now = play(turns, lines, now + 3000)
            assert turns.poll(now + 3000) == []
            lines = turns.poll(now + 3000 + 8000)
        elif how == 'hangup':
            # A disconnected holder returns without a choice; no text shortcut in scripted mode.
            lines = engine.on_handoff(False)
            engine._finish()
        else:
            lines = engine.on_handoff(False)
        result(engine, lines, 'needs_follow_up')
        assert engine.on_handoff(False) == []
        assert engine.confirmation_number() is None

    from tests.lane_B.test_sc3_scripted_carrier import connect_rig
    from tests.lane_B.test_h3b3_handoff_line import reach_handoff
    async with connect_rig(audio_rig) as (_, call, conn):
        await reach_handoff(conn, audio_rig)
        closed = []
        original = call._engine.on_handoff
        def leaving(joined):
            lines = original(joined)
            closed.extend(lines)
            return lines
        monkeypatch.setattr(call._engine, 'on_handoff', leaving)
        await call.hangup()
        result(call._engine, closed, 'needs_follow_up')
        assert call._engine.ended and not call._scripted_turns.in_handoff_window


@pytest.fixture
def audio_rig(company_dir, tmp_path, engine_config, mode_env, monkeypatch):
    # Reuse the offline carrier fixture without introducing module import cycles.
    from tests.lane_B.test_sc3_scripted_carrier import rig
    from tests.company._scenario_data import edit_scenario_yaml
    data = rig.__wrapped__(company_dir, tmp_path, engine_config, mode_env, monkeypatch)
    edit_scenario_yaml(data[0].company_dir, 'baseline', lambda d: d['steps']['rep']['scripted'].update(
        holder_prompt_ms=600, holder_max_asks=2, resume_wait_ms=1500))
    return data


async def test_u112_win_5_keys_that_are_not_a_choice(company_dir, engine_config, audio_rig, monkeypatch):
    engine, turns, _, lines, now = window(company_dir, engine_config)
    assert keypad(engine, turns, '1') == []  # Before DOB playback.
    now = play(turns, lines, now)
    assert keypad(engine, turns, '0102199') == []
    lines = keypad(engine, turns, '0#1')
    assert [line.text for line in lines] == [OK, DEPOSIT]
    assert keypad(engine, turns, '1') == []  # Deposit is queued, not started.
    now = play(turns, lines, now)
    assert keypad(engine, turns, '#') == []  # DOB terminator is never a choice.
    last = speak(turns, now + 20, 200)
    assert turns.poll(last + 1500) == []  # Voice/volume cannot consent.
    lines = keypad(engine, turns, '3')
    assert [line.text for line in lines] == [DEPOSIT]
    now = play(turns, lines, last + 1500)
    assert keypad(engine, turns, '3') == []
    result(engine, engine.on_handoff(False), 'needs_follow_up')
    engine, turns, _, now = deposit(company_dir, engine_config)
    assert [line.text for line in keypad(engine, turns, '1')] == [AGREE]
    assert keypad(engine, turns, '1') == []
    assert keypad(engine, turns, '2') == []
    result(engine, engine.on_handoff(False), 'confirmed')

    engine, turns, _, _ = deposit(company_dir, engine_config)
    turns.lines_replaced()
    before = dict(engine._holder_results)
    assert engine.holder_input(keys='1') == []
    assert engine._holder_results == before

    # Queuing/synthesizing a question is earlier than actual playback. A key
    # sent during slow synthesis must not consent, including a held candidate.
    from tests.lane_B.test_sc3_scripted_carrier import connect_rig, wait_line, speak as line_speak
    from tests.lane_B.test_h3b3_handoff_line import reach_handoff
    from tests.company.test_audio_carrier_line import send_keys
    from tests.company._live_harness import wait_until
    polly, times = audio_rig[2:4]
    started, release = threading.Event(), threading.Event()
    original = polly.synthesize_speech
    def slow(**kwargs):
        if kwargs['Text'] == DEPOSIT:
            started.set()
            assert release.wait(6)
        return original(**kwargs)
    monkeypatch.setattr(polly, 'synthesize_speech', slow)
    try:
        async with connect_rig(audio_rig) as (_, call, conn):
            await reach_handoff(conn, audio_rig)
            await send_keys(conn, '01021990#')
            await wait_until(started.is_set, 6, 'deposit synthesis')
            await send_keys(conn, '1')
            assert call._engine._holder_results.get(1) is None
            from tests.company._signals import dtmf_tone, silence, ulaw_encode
            async def send_pcm(pcm):
                wire = ulaw_encode(pcm)
                for offset in range(0, len(wire), 160):
                    await conn.send({'event': 'media', 'streamSid': conn.stream_sid,
                                     'media': {'payload': base64.b64encode(wire[offset:offset + 160]).decode('ascii')}})
                    await asyncio.sleep(0.02)
            held = dtmf_tone('1', 500)
            await send_pcm(held[:320])  # Candidate begins before playback.
            await asyncio.sleep(0.05)
            release.set()
            await send_pcm(held[320:] + silence(100))
            await wait_line(conn, polly, times, DEPOSIT)
            assert call._engine._holder_results.get(1) is None
            await send_keys(conn, '2')
            await wait_line(conn, polly, times, DECLINE)
            await line_speak(conn, 200)
            await wait_line(conn, polly, times, CLOSE)
            await wait_until(lambda: conn.closed_at is not None, 6, 'follow-up stop')
            assert call._engine.ended and call._engine.confirmation_number() is None
            assert AGREE not in [entry['Text'] for entry in polly.calls]
    finally:
        release.set()


def test_u112_win_6_privacy(company_dir, engine_config, caplog):
    caplog.set_level(logging.INFO)
    engine, turns, _, now = deposit(company_dir, engine_config)
    lines = keypad(engine, turns, '1')
    play(turns, lines, now)
    keypad_messages = [r.getMessage() for r in caplog.records if 'keypad' in r.getMessage() or 'choice' in r.getMessage()]
    assert keypad_messages == ['scripted holder keypad entry complete', 'scripted holder choice recorded']
    assert '01021990' not in caplog.text
    assert all(word not in caplog.text.lower() for word in ('agree', 'decline'))
    assert not any(c.isdigit() for message in keypad_messages for c in message)
    # Goertzel candidate/held tones must not enter the speech endpoint.
    from tests.company._signals import dtmf_tone
    from waitless.company.dtmf_detect import GoertzelDetector
    _, turns, _, lines, now = window(company_dir, engine_config)
    now = play(turns, lines, now)
    detector = GoertzelDetector(sample_rate=8000)
    pcm = dtmf_tone('1', 2000)
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


async def test_u112_win_7_card_does_not_answer_for_holder(company_dir, engine_config, mode_env, fake_clock, test_profile):
    from e_support import (build_settings, make_compiled, make_rig, make_task_card,
                           run_until, spec_handoff, spec_end)
    from waitless.models import PrecallDecision
    for selected in ('approve', 'ask_me', None):
        for key in ('1', '2', None):
            engine, turns, scenario, req = request(company_dir, engine_config)
            turns.line_finished(req, 0)
            last = speak(turns, 100, 200)
            opened = last + engine_config.handoff.announce_idle_ms
            turns.poll(opened)
            # This fixture feeds TextCall; expose its unchanged legacy request.
            from waitless.company.engine import _text_scenario
            legacy = _text_scenario(scenario).steps['rep'].text_script[1]
            req = req.model_copy(update={'text': legacy.say, 'expect': legacy.expect})
            class Company:
                scenario_id = 'baseline'
                ended = property(lambda self: engine.ended)
                start = lambda self: [req]
                on_speech = lambda self, text: engine.on_speech(text)
                on_handoff = lambda self, joined: (turns.poll(opened + 3000) if joined else engine.on_handoff(False))
            card = make_task_card()
            card.precall_decisions = [] if selected is None else [PrecallDecision(
                question_id='deposit_consent', question='The holder decides on the call.',
                options=[{'id': 'approve', 'label': 'Willing', 'detail': ''}, {'id': 'ask_me', 'label': 'Ask me', 'detail': ''}],
                default=None, selected=selected, needs_user=False)]
            rig = make_rig(settings=build_settings(mode_env('text-sim'), engine_config), clock=fake_clock,
                           company=Company(), allowed_fields=test_profile,
                           compiled=make_compiled(card, test_profile),
                           script=[('bring them onto the line', spec_handoff()),
                                   ('confirmation number', spec_end())])
            session = await rig.start()
            await run_until(fake_clock, lambda: session.status().phase == 'handoff_ready', what='handoff')
            joined = await session.join()
            now = play(turns, turns._questions, opened + 3000)
            lines = keypad(engine, turns, '01021990#')
            now = play(turns, lines, now)
            if key is not None:
                play(turns, keypad(engine, turns, key), now)
            await session.return_to_agent(joined.handoff_id)
            await run_until(fake_clock, lambda: session.ended, what='call end')
            assert bool(engine.confirmation_number()) == (key == '1')
            outcome = 'confirmed' if key == '1' else 'needs_follow_up'
            assert any(line.expect and line.expect.outcome == outcome for line in session.lines())


def test_u112_win_8_check_steps_validates_choice(company_dir, engine_config):
    _, _, company, scenario = setup_turns(company_dir, engine_config)
    check_scripted_steps(scenario, company)
    invalid = [
        {'keypad_choice': {'12': 'agree'}},
        {'keypad_choice': {'A': 'agree'}},
        {'keypad_choice': {'1': 'missing'}},
        {'choice_lines': {'agree': '', 'decline': DECLINE}},
        {'choice_lines': {'agree': AGREE, 'decline': ' '}},
        {'choice_lines': {'agree': AGREE, 'decline': DECLINE, 'extra': 'Unused'}},
        {'not_agreed_close': None},
    ]
    for change in invalid:
        modified = scenario.model_copy(deep=True)
        action = next(a for a in modified.steps['rep'].actions if getattr(a, 'keypad_choice', None))
        for name, value in change.items():
            setattr(action, name, value)
        with pytest.raises(ScenarioError):
            check_scripted_steps(modified, company)
