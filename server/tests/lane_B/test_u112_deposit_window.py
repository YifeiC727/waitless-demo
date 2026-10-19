import logging

import pytest

from fakes.polly import tone_pcm16
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
        if line.expect and line.expect.intent == 'request_handoff':
            return engine, turns, scenario, line
        if line.expect.intent.startswith('press_field:'):
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


def test_u112_win_4_unanswered_or_early_return(company_dir, engine_config):
    for how in ('unanswered', 'early_return', 'hangup', 'dob_only'):
        if how in ('early_return', 'hangup'):
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


def test_u112_win_5_keys_that_are_not_a_choice(company_dir, engine_config):
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
            class Company:
                scenario_id = 'baseline'
                ended = property(lambda self: engine.ended)
                start = lambda self: [req]
                on_speech = lambda self, text: engine.on_speech(text)
                on_handoff = lambda self, joined: (turns.poll(opened + 3000) if joined else engine.on_handoff(False))
            card = make_task_card()
            card.precall_decisions = [] if selected is None else [PrecallDecision(
                question_id='deposit_consent', question='The holder decides on the call.',
                options=[{'id': 'approve', 'label': 'Willing'}, {'id': 'ask_me', 'label': 'Ask me'}],
                default=None, selected=selected)]
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
            assert session.status().outcome == ('confirmed' if key == '1' else 'needs_follow_up')


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
