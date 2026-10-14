import logging

import pytest
from tests.company._scenario_data import copy_company_root, edit_scenario_yaml
from tests.lane_B.test_sc2_scripted_turns import setup_turns, agent_batch, speak, VOICE, SILENCE
from waitless.company.scripted import ReplyWaiter
from waitless.models import CompanyLine

QUESTION = 'Please confirm your date of birth.'


def open_window(root, config):
    engine, turns, company, _ = setup_turns(root, config)
    agent_batch(engine)
    engine.on_speech('reply')
    engine.on_speech('reply')
    engine.on_digits(company.customers[0].account_number + '#')
    request, = engine.on_speech('reply')
    turns.line_finished(request, 0)
    assert turns.phase == 'announce'
    last = speak(turns, 100, 1000)
    assert turns.poll(last + config.handoff.announce_idle_ms - 1) == []
    assert not turns.in_handoff_window
    window = last + config.handoff.announce_idle_ms
    assert turns.poll(window) == []
    assert turns.in_handoff_window and turns.phase == 'holder_wait'
    return engine, turns, window


def ask(turns, window, prompt_ms=3000):
    assert turns.poll(window + prompt_ms - 1) == []
    question, = turns.poll(window + prompt_ms)
    assert question.kind == 'speech' and question.role == 'rep' and question.text == QUESTION
    assert turns.phase == 'asking' and turns.in_handoff_window and turns.counts_holder_audio
    return question


def finish_question(turns, question, now):
    turns.line_started(question)
    assert turns.phase == 'asking' and turns.in_handoff_window
    turns.line_finished(question, now)
    assert turns.phase == 'holder_answer' and turns.in_handoff_window
    assert turns.counts_holder_audio


def answer(turns, now):
    last = speak(turns, now, 1000)
    assert turns.poll(last + 1499) == []
    assert turns.phase == 'holder_answer'
    entered = last + 1500
    assert turns.poll(entered) == []
    assert turns.phase == 'resume_wait' and turns.in_handoff_window and not turns.counts_holder_audio
    return entered


def assert_deposit(turns, now):
    deposit, = turns.poll(now)
    assert deposit.text.startswith('Verification is complete.')
    assert turns.phase == 'idle' and not turns.in_handoff_window and not turns.counts_holder_audio
    return deposit


def test_waiter_heard(engine_config):
    waiter = ReplyWaiter(threshold_dbfs=engine_config.vad.threshold_dbfs)
    assert not waiter.heard
    waiter.feed(VOICE, 0)
    waiter.arm(end_silence_ms=1500, now_ms=10)
    assert not waiter.heard
    waiter.feed(VOICE, 9)
    assert not waiter.heard
    waiter.feed(SILENCE, 10)
    assert not waiter.heard
    waiter.feed(VOICE, 11)
    assert waiter.heard
    waiter.disarm()
    assert not waiter.heard
    waiter.arm(end_silence_ms=1500, now_ms=20)
    assert not waiter.heard


def test_timer_asks(company_dir, engine_config):
    _, turns, window = open_window(company_dir, engine_config)
    question = ask(turns, window)
    # Playback never times out, even when synthesis/playback takes a long time.
    assert turns.poll(window + 100000) == [] and turns.phase == 'asking'
    finish_question(turns, question, window + 100100)


def test_holder_speaks_first(company_dir, engine_config):
    _, turns, window = open_window(company_dir, engine_config)
    last = speak(turns, window + 500, 500)
    assert turns.poll(last + 1499) == []
    question, = turns.poll(last + 1500)
    assert question.text == QUESTION and turns.phase == 'asking'
    assert last + 1500 < window + 3000


def test_answer_resume_deposit(company_dir, engine_config, caplog):
    caplog.set_level(logging.INFO)
    _, turns, window = open_window(company_dir, engine_config)
    question = ask(turns, window)
    finish_question(turns, question, window + 3100)
    entered = answer(turns, window + 3200)
    last = speak(turns, entered + 100, 1000)
    assert turns.poll(last + 1499) == []
    assert_deposit(turns, last + 1500)
    messages = [r.getMessage() for r in caplog.records]
    assert messages.count('scripted holder answered') == 1
    assert 'scripted handoff window closed step=rep reason=resume' in messages


@pytest.mark.parametrize('offset_ms', [0, 5])
def test_resume_voice_at_answer_deadline_before_poll(company_dir, engine_config, caplog, offset_ms):
    caplog.set_level(logging.INFO)
    _, turns, window = open_window(company_dir, engine_config)
    question = ask(turns, window)
    finish_question(turns, question, window + 3100)
    last = speak(turns, window + 3200, 1000)
    due = last + 1500
    # Carrier feeds and counts this frame before its next poll.
    assert turns.feed(VOICE, due + offset_ms)
    assert turns.phase == 'resume_wait'
    assert not turns.counts_holder_audio and turns.in_handoff_window
    assert turns.poll(due + 10) == []
    end = speak(turns, due + 25, 500)
    assert turns.poll(end + 1499) == []
    assert_deposit(turns, end + 1500)
    messages = [r.getMessage() for r in caplog.records]
    assert messages.count('scripted holder answered') == 1
    assert messages.count('scripted handoff window closed step=rep reason=resume') == 1


def test_reasks_bounded_and_join_once(company_dir, engine_config, monkeypatch, caplog):
    caplog.set_level(logging.INFO)
    engine, turns, window = open_window(company_dir, engine_config)
    calls = []
    original = engine.on_handoff
    def handoff(joined):
        calls.append(joined)
        return original(joined)
    monkeypatch.setattr(engine, 'on_handoff', handoff)
    question = ask(turns, window)
    now = window + 3100
    for n in range(1, 5):
        finish_question(turns, question, now)
        turns.feed(SILENCE, now + 20)
        assert turns.poll(now + 2999) == []
        if n < 4:
            repeated, = turns.poll(now + 3000)
            assert repeated is question and turns.phase == 'asking'
            now += 3100
        else:
            assert turns.poll(now + 3000) == []
            assert turns.phase == 'resume_wait' and not turns.counts_holder_audio
    entered = now + 3000
    assert turns.poll(entered + 7999) == []
    assert_deposit(turns, entered + 8000)
    assert calls == [True, False]
    messages = [r.getMessage() for r in caplog.records]
    assert [m for m in messages if 'scripted handoff ask' in m] == [
        f'scripted handoff ask n={n} step=rep' for n in range(1, 5)]
    assert messages.count('scripted holder no answer') == 1
    assert 'scripted handoff window closed step=rep reason=idle' in messages


def test_resume_idle_limit(company_dir, engine_config):
    _, turns, window = open_window(company_dir, engine_config)
    question = ask(turns, window)
    finish_question(turns, question, window + 3100)
    entered = answer(turns, window + 3200)
    turns.feed(SILENCE, entered + 20)
    assert turns.poll(entered + 7999) == []
    assert_deposit(turns, entered + 8000)


def test_question_playback_voice_not_answer(company_dir, engine_config):
    _, turns, window = open_window(company_dir, engine_config)
    question = ask(turns, window)
    turns.line_started(question)
    last = speak(turns, window + 3100, 500)
    assert turns.poll(last + 100000) == []
    end = last + 100100
    turns.line_finished(question, end)
    turns.feed(SILENCE, end + 20)
    assert turns.poll(end + 1500) == [] and turns.phase == 'holder_answer'


def test_configured_timers(company_dir, tmp_path, engine_config):
    root = copy_company_root(company_dir, tmp_path)
    edit_scenario_yaml(root, 'baseline', lambda d: d['steps']['rep']['scripted'].update(
        holder_prompt_ms=500, resume_wait_ms=900))
    _, turns, window = open_window(root, engine_config)
    question = ask(turns, window, 500)
    finish_question(turns, question, window + 600)
    entered = answer(turns, window + 700)
    assert turns.poll(entered + 899) == []
    assert_deposit(turns, entered + 900)


@pytest.mark.parametrize('phase', ['holder_wait', 'holder_answer', 'resume_wait'])
def test_started_voice_prevents_idle_deadline(company_dir, engine_config, phase):
    _, turns, window = open_window(company_dir, engine_config)
    entered = window
    if phase != 'holder_wait':
        question = ask(turns, window)
        finish_question(turns, question, window + 3100)
        entered = window + 3100
        if phase == 'resume_wait':
            entered = answer(turns, entered + 100)
    # Ongoing voice across the idle deadline must still wait for its endpoint.
    last = speak(turns, entered + 100, 10000)
    assert turns.poll(last) == [] and turns.phase == phase
    assert turns.poll(last + 1499) == [] and turns.phase == phase
    result = turns.poll(last + 1500)
    assert turns.phase == {'holder_wait': 'asking', 'holder_answer': 'resume_wait', 'resume_wait': 'idle'}[phase]
    assert bool(result) == (phase != 'holder_answer')


def test_empty_question_enters_answer_without_playback(company_dir, engine_config, monkeypatch):
    engine, turns, window = open_window(company_dir, engine_config)
    original = engine.on_handoff
    def empty(joined):
        result = original(joined)
        return [] if joined else result
    monkeypatch.setattr(engine, 'on_handoff', empty)
    assert turns.poll(window + 3000) == []
    assert turns.phase == 'holder_answer' and turns.in_handoff_window
    assert turns.poll(window + 5999) == [] and turns.phase == 'holder_answer'
    entered = answer(turns, window + 6100)
    assert_deposit(turns, entered + 8000)


def test_multi_question_identity_and_replacement(company_dir, engine_config, monkeypatch):
    engine, turns, window = open_window(company_dir, engine_config)
    original = engine.on_handoff
    extra = CompanyLine(step_id='rep', kind='speech', role='rep', text='Another question')
    def multiple(joined):
        result = original(joined)
        return result + [extra] if joined else result
    monkeypatch.setattr(engine, 'on_handoff', multiple)
    questions = turns.poll(window + 3000)
    assert len(questions) == 2
    for line in questions:
        turns.line_started(line)
        assert turns.in_handoff_window and turns.phase == 'asking'
    turns.line_finished(questions[-1], window + 3100)
    assert turns.phase == 'holder_answer'
    # Equality alone does not identify cached question objects.
    turns.line_started(extra.model_copy())
    assert not turns.in_handoff_window and turns.phase == 'idle'
    assert turns.poll(window + 100000) == []
