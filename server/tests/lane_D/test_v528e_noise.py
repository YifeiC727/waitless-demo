"""13.2: unsealed noise segments expire by BOTH tail clocks, before windowing."""
import json
import pytest
from call_engine._support import settle
from lane_D.test_v528e_segments import guarded, chunk
from lane_v528c2.test_wiring import (live528, reply, turn, speaker_decisions,
    agent_call, settings, spec, engine, compiled, line_number, store, trace)

@pytest.mark.parametrize('axis', ['audio', 'wall'])
@pytest.mark.parametrize('duration', [9999, 10000, 10001])
async def test_noise_tail_dual_boundary(live528, axis, duration):
    h = live528; await guarded(h)
    await turn(h, 'Isolated recognition')
    await chunk(h, duration if axis == 'audio' else 10000,
        duration if axis == 'wall' else 10000, value=4000)
    events = h.events('hold_noise_dropped')
    assert bool(events) == (duration >= 10000)
    assert len(speaker_decisions(h)) == 1
    if events:
        event, = events
        assert event.action == {'seqs': [2]}
        assert event.transcript is None and event.policy_decision is None
        assert event.event_type != 'status_updated'
        await chunk(h, 20, 20, value=4000)
        assert len(h.events('hold_noise_dropped')) == 1

async def test_drop_filters_before_eight_line_window_and_both_requests(live528):
    h = live528; reply(h)
    for n in range(8): await turn(h, f'Earlier history {n}')
    await guarded(h)
    await turn(h, 'Isolated recognition alpha'); await turn(h, 'Isolated recognition beta')
    await chunk(h, 10000, 10000, value=4000)
    assert h.events('hold_noise_dropped')[-1].action == {'seqs': [10, 11]}
    assert [e.action['seq'] for e in h.events('company_turn')][-2:] == [10, 11]
    reply(h, transfer='none')
    requests = {}
    ask = h.call._keypad.deps.jev.ask
    async def capture(state, questions):
        requests['main' if 'speaker' in questions else 'transfer'] = [
            line.text for line in state.transcript.lines]
        return await ask(state, questions)
    h.call._keypad.deps.jev.ask = capture
    await turn(h, 'Current company sentence. Last sentence.')
    await h.silence(600)
    d = speaker_decisions(h)[-1]
    main = [f'Earlier history {n}' for n in range(2, 8)] + [
        'Transfer announcement', 'Current company sentence. Last sentence.']
    transfer = main[1:-1] + ['Current company sentence.', 'Last sentence.']
    assert requests == {'main': main, 'transfer': transfer}
    for name, expected in (('state', main), ('transfer_state', transfer)):
        lines = d.jev_request[name]['transcript']['lines']
        assert len(lines) == 8 and [line['text'] for line in lines] == expected
    # Arbitrary later state queries also filter these sources, including their own cutoff.
    for seq in (10, 11, 12, None):
        assert not any('Isolated recognition' in line.text
            for line in h.call._keypad._state(seq).transcript.lines)

async def test_new_semantic_tail_resets_both_deadlines_empty_does_not(live528):
    h = live528; await guarded(h); await turn(h, 'First recognition')
    await chunk(h, 6000, 6000, value=4000)
    await turn(h, 'Second recognition')
    await chunk(h, 4000, 4000, value=4000); await turn(h, '')
    assert not h.events('hold_noise_dropped')
    await chunk(h, 6000, 6000, value=4000)
    assert h.events('hold_noise_dropped')[-1].action == {'seqs': [2, 3]}
    assert [d.action['turn_seq'] for d in speaker_decisions(h)] == [1, 4]

async def test_only_current_six_are_dropped_slid_out_rows_remain_history(live528):
    h = live528; await guarded(h)
    for n in range(8): await turn(h, f'Recognition {n}')
    await chunk(h, 10000, 10000, value=4000)
    assert h.events('hold_noise_dropped')[-1].action == {'seqs': [4, 5, 6, 7, 8, 9]}
    lines = [line.text for line in h.call._keypad._state().transcript.lines]
    assert lines[-2:] == ['Recognition 0', 'Recognition 1']

@pytest.mark.parametrize('quiet_ms', [600, 10000])
async def test_quiet_wins_over_noise_deadline_and_sealed_inflight_never_drops(live528, quiet_ms):
    from lane_D.test_v528d_requests import SplitWire
    h = live528; await guarded(h)
    h.settings.engine.speaker.guard_segment_quiet_ms = quiet_ms
    h.settings.engine.speaker.verdict_timeout_ms = 30000
    wire = SplitWire(); h.call._keypad.deps.jev = wire
    await turn(h, 'Company sentence')
    await chunk(h, quiet_ms, quiet_ms)
    assert set(wire.calls) == {'main', 'transfer'}
    assert not h.events('hold_noise_dropped')
    await chunk(h, 10000, 10000, value=4000)
    assert h.call._keypad._active is not None
    assert not h.events('hold_noise_dropped')
    wire.release['main'].set(); wire.release['transfer'].set(); await settle(300)

async def test_noise_uses_config_and_provider_time_cannot_move_tail(live528):
    from waitless import models as m
    h = live528; await guarded(h)
    h.settings.engine.speaker.guard_noise_after_ms = 40
    await h.call._tap.on_provider_text(m.ProviderTextEvent(role='USER', stage='FINAL',
        text='Recognition', stop_reason='END_TURN', t_ms=900000))
    await chunk(h, 20, 20, value=4000); assert not h.events('hold_noise_dropped')
    await chunk(h, 20, 20, value=4000)
    assert h.events('hold_noise_dropped')[-1].action == {'seqs': [2]}

async def test_noise_trace_store_roundtrip_and_example(live528, tmp_path):
    from waitless.trace import Trace
    from waitless.store import Store
    h = live528; await guarded(h); await turn(h, 'Recognition')
    await chunk(h, 10000, 10000, value=4000)
    event, = h.events('hold_noise_dropped')
    db = Store(tmp_path / 'noise.sqlite')
    try:
        trace = Trace(db, h.clock); trace.event(event)
        got, = trace.events(call_id=h.spec.call_id)
        assert got.event_type == 'hold_noise_dropped' and got.action == {'seqs': [2]}
        assert got.transcript is None and got.policy_decision is None
    finally: db.close()
    print('V528_EXAMPLE noise ' + json.dumps({'event_type': event.event_type,
        'action': event.action, 'transcript': event.transcript}))
