"""12.1(2): actual request windows and source-aware backfill."""
import pytest
from fakes import FakeJev
from waitless import models as m
from lane_v528a.test_keypad_policy import (live528, script, agent_call, settings, spec,
    engine, compiled, line_number, store, trace)

ANNOUNCEMENT = 'Let me connect you. Thank you for holding.'

def row(h, kind, text, seq=None, *, t_ms=0, identity=None):
    action = {'seq': seq} if kind == 'company_turn' else {'response_identity': identity}
    h.trace.event(m.TraceEvent(task_id=h.spec.task_id, call_id=h.spec.call_id,
        event_type=kind, speaker='company' if kind == 'company_turn' else 'waitless',
        transcript=text, action=action, t_ms=t_ms))

async def prepare(h, n, text=ANNOUNCEMENT):
    h.call._keypad.deps.jev = FakeJev(default=script(key='none'))
    for seq in range(1, n + 1):
        row(h, 'company_turn', text if seq == n else f'History {seq}', seq)
    g, k = h.call._media_gate, h.call._keypad
    g.begin_pending(n); k.begin_turn(n)
    await g.clear_output()
    return k

def window(call):
    return [(line.speaker, line.text) for line in call.state.transcript.lines]

@pytest.mark.parametrize('n', [1, 2, 7, 8])
async def test_split_replace_before_truncation_and_main_immutable(live528, n):
    h = live528; k = await prepare(h, n)
    await k._decide(n)
    calls = k.deps.jev.calls
    main = [('company', f'History {seq}') for seq in range(1, n)] + [('company', ANNOUNCEMENT)]
    transfer = ([('company', f'History {seq}') for seq in range(2 if n == 8 else 1, n)]
                + [('company', 'Let me connect you.'), ('company', 'Thank you for holding.')])
    assert window(calls[0]) == main
    assert window(calls[1]) == transfer
    assert h.events('company_turn')[-1].transcript == ANNOUNCEMENT
    d = h.events('decision')[0]
    assert d.jev_request['state'] == calls[0].state.model_dump(mode='json')
    assert d.jev_request['transfer_state'] == calls[1].state.model_dump(mode='json')

@pytest.mark.parametrize('text,expected', [
    ('no punctuation', ['no punctuation']),
    ('please hold...', ['please hold...']),
    ('this is mr. chen.', ['this is mr. chen.']),
    ('go to st. paul.', ['go to st. paul.']),
    ('use e.g. this.', ['use e.g. this.']),
    ('this is DR. chen.', ['this is DR. chen.']),
    ('one.two.', ['one.two.']),
    ('One? Two! Three.', ['One? Two!', 'Three.']),
    (' One.   Last. ', ['One.', 'Last.']),
    ('One. Two. Three. Four. Five. Six. Seven. Eight. Nine. Last.',
     ['One. Two. Three. Four. Five. Six. Seven. Eight. Nine.', 'Last.']),
])
async def test_split_points_and_more_than_eight_sentences(live528, text, expected):
    h = live528; k = await prepare(h, 1, text)
    await k._decide(1)
    assert [line.text for line in k.deps.jev.calls[0].state.transcript.lines] == [text]
    assert [line.text for line in k.deps.jev.calls[1].state.transcript.lines] == expected

@pytest.mark.parametrize('position', ['first', 'last'])
async def test_waitless_rows_never_split_or_trigger_penultimate_split(live528, position):
    h = live528
    row(h, 'waitless_utterance', 'Agent first. Agent last.', t_ms=0 if position == 'first' else 2)
    row(h, 'company_turn', ANNOUNCEMENT, 1, t_ms=1)
    g, k = h.call._media_gate, h.call._keypad
    k.deps.jev = FakeJev(default=script(key='none'))
    g.begin_pending(1); k.begin_turn(1); await g.clear_output(); await k._decide(1)
    lines = window(k.deps.jev.calls[1])
    assert ('waitless', 'Agent first. Agent last.') in lines
    if position == 'first':
        assert lines == [('waitless', 'Agent first. Agent last.'), ('company', 'Let me connect you.'),
                         ('company', 'Thank you for holding.')]
    else:
        assert lines == [('company', ANNOUNCEMENT), ('waitless', 'Agent first. Agent last.')]

@pytest.mark.parametrize('last_decided,selected', [(0, 1), (1, 7)])
async def test_full_window_unjudged_edge_not_proved_by_same_seq_waitless(live528, last_decided, selected):
    h = live528
    row(h, 'company_turn', 'Unjudged announcement', 1)
    h.trace.event(m.TraceEvent(task_id=h.spec.task_id, call_id=h.spec.call_id,
        event_type='response_bound', action={'identity': '1:1', 'seq': 1, 'epoch': 1}, t_ms=0))
    row(h, 'waitless_utterance', 'Agent row', identity='1:1')
    for seq in range(2, 8):
        row(h, 'company_turn', ANNOUNCEMENT if seq == 7 else f'History {seq}', seq)
    k = h.call._keypad; k._last_decided_seq = last_decided
    assert k._next_seq(7) == selected
    assert [(line.text, source) for _, line, source in k._state_entries(7)
            if line.speaker == 'waitless'] == [('Agent row', 1)]

@pytest.mark.parametrize('n', [8, 9, 20, 100])
@pytest.mark.parametrize('mixed', [False, True])
async def test_fixed_backlog_strict_progress_bounded_by_company_count(live528, n, mixed):
    h = live528; k = h.call._keypad
    for seq in range(1, n + 1):
        if mixed: row(h, 'waitless_utterance', 'Old agent row')
        row(h, 'company_turn', f'Company {seq}. Last sentence.', seq)
    k.deps.jev = FakeJev(default=script(key='none'))
    h.call._media_gate.begin_pending(n); k.begin_turn(n)
    await h.call._media_gate.clear_output()
    seen = []
    while k._last_decided_seq < n and len(seen) <= n:
        selected = k._next_seq(n)
        assert selected > k._last_decided_seq
        seen.append(selected)
        await k._decide(selected)
        transfer = window(k.deps.jev.calls[-1])
        assert transfer[-2:] == [('company', f'Company {selected}.'), ('company', 'Last sentence.')]
        assert k._last_decided_seq == selected
    assert seen[-1] == n and len(seen) <= n

async def test_successful_key_row_keeps_origin_and_is_never_split(live528):
    h = live528; k = h.call._keypad
    k.deps.jev = FakeJev(default=script(key='*'))
    await h.sonic.emit_user_text('Press star. To reach a person.')
    from call_engine._support import settle
    await settle(250)
    row(h, 'company_turn', ANNOUNCEMENT, 2)
    entries = k._state_entries(2)
    key_rows = [(line.text, source) for _, line, source in entries if line.text.startswith('[keys pressed:')]
    assert key_rows == [('[keys pressed: *]', 1)]
    k.deps.jev = FakeJev(default=script(key='none'))
    h.call._media_gate.begin_pending(2); k.begin_turn(2)
    await h.call._media_gate.clear_output(); await k._decide(2)
    assert ('waitless', '[keys pressed: *]') in window(k.deps.jev.calls[1])

async def test_repeated_completed_seq_never_redecides(live528):
    h = live528; k = await prepare(h, 1, 'One sentence')
    await k.on_company_turn(1)
    await k.on_company_turn(1)
    assert len(k.deps.jev.calls) == 2

async def test_single_event_snapshot_used_for_both_requests(live528):
    from unittest.mock import patch
    h = live528; k = await prepare(h, 1)
    with patch.object(k, '_events', wraps=k._events) as read:
        await k._decide(1)
        assert read.call_count == 1
