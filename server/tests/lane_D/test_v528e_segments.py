"""13.2 guarded segments: real LiveCall, inbound MediaGate, tap and QuietMeter."""
import asyncio
import json
import pytest
from call_engine._support import settle, in_frame, zero_pcm16
from lane_D.test_v528d_requests import SplitWire
from lane_v528c2.test_wiring import (live528, reply, turn, speaker_decisions,
    agent_call, settings, spec, engine, compiled, line_number, store, trace)

async def guarded(h):
    reply(h, transfer='pending'); await turn(h, 'Transfer announcement')
    assert h.call._keypad.queued

async def chunk(h, audio_ms, wall_ms, *, value=0):
    import struct
    await h.clock.advance(wall_ms)
    pcm = struct.pack('<h', value) * (h.rate * audio_ms // 1000)
    await h.transport.input().feed(in_frame(pcm, h.rate)); await settle(100)

@pytest.mark.parametrize('axis', ['audio', 'wall'])
@pytest.mark.parametrize('duration', [599, 600, 601])
async def test_segment_quiet_dual_boundary(live528, axis, duration):
    h = live528; await guarded(h); reply(h, transfer='none')
    await turn(h, 'First clause'); await turn(h, 'Last clause')
    assert len(speaker_decisions(h)) == 1
    await chunk(h, duration if axis == 'audio' else 600,
        duration if axis == 'wall' else 600)
    assert len(speaker_decisions(h)) == (1 if duration == 599 else 2)
    if duration >= 600:
        p = speaker_decisions(h)[-1].policy_decision
        assert p['segment_seqs'] == [2, 3] and p['segment_trigger'] == 'quiet'
        assert h.call._keypad._last_decided_seq == 3

@pytest.mark.parametrize('count', [1, 4, 6, 7, 8, 12])
async def test_music_never_decides_and_sliding_six_no_backfill(live528, count):
    h = live528; await guarded(h); reply(h, transfer='none')
    for n in range(count):
        await turn(h, f'Company clause {n}. Tail clause {n}.')
        await h.speech(100)
        assert len(speaker_decisions(h)) == 1
    await h.silence(600); await settle(300)
    ds = speaker_decisions(h)
    assert [d.action['turn_seq'] for d in ds] == [1, count + 1]
    seqs = list(range(max(2, count - 4), count + 2))
    assert ds[-1].policy_decision['segment_seqs'] == seqs
    assert len(seqs) <= 6 and h.call._keypad._last_decided_seq == count + 1
    for key in ('state', 'transfer_state'):
        lines = ds[-1].jev_request[key]['transcript']['lines']
        assert len(lines) <= 8
        for seq in seqs:
            assert any(f'clause {seq - 2}.' in line['text'] for line in lines)
    await h.silence(600); assert len(speaker_decisions(h)) == 2

async def test_empty_every_500ms_does_not_delay_or_duplicate_handoff(live528):
    h = live528; await guarded(h); reply(h, transfer='connected')
    await turn(h, 'Representative greeting')
    await h.silence(500); mark = h.call._quiet.marks[2]
    await turn(h, '')
    assert h.call._quiet.marks[2] is mark and 3 not in h.call._quiet.marks
    await h.silence(100); await settle(500)
    assert len(h.events('handoff_ready')) == 1
    for duration in (400, 500, 500):
        await h.silence(duration); await turn(h, '')
        assert len(h.events('handoff_ready')) == 1
    real = [d for d in speaker_decisions(h) if not d.policy_decision['no_jev']]
    assert [d.action['turn_seq'] for d in real] == [1, 2]

@pytest.mark.parametrize('empty', [False, True])
async def test_inflight_new_final_stales_old_but_empty_keeps_result(live528, empty):
    h = live528; await guarded(h)
    wire = SplitWire(); wire.answer['transfer']['choice'] = 'connected'
    h.call._keypad.deps.jev = wire
    await turn(h, 'First representative sentence'); await h.silence(600)
    assert set(wire.calls) == {'main', 'transfer'}
    await turn(h, '' if empty else 'New representative sentence')
    wire.release['main'].set(); wire.release['transfer'].set(); await settle(500)
    if empty:
        assert len(h.events('handoff_ready')) == 1
    else:
        assert not h.events('handoff_ready')
        reply(h, transfer='connected'); await h.silence(600); await settle(500)
        assert len(h.events('handoff_ready')) == 1
        assert speaker_decisions(h)[-1].policy_decision['segment_seqs'] == [3]

@pytest.mark.parametrize('locked', [False, True])
async def test_g0_empty_preserves_fallback_even_waiting_for_lock(live528, locked):
    h = live528; await guarded(h); reply(h, transfer='none')
    await turn(h, 'Unclear sentence'); await h.silence(600)
    candidate = h.call._quiet_candidate; assert candidate is not None
    if locked: await h.call._handoff_lock.acquire()
    try:
        await h.silence(4400 if locked else 400)
        if locked: assert candidate.inflight is not None
        before = (candidate.seq, candidate.mark, candidate.token, candidate.source_decision_id)
        await turn(h, '')
        assert h.call._quiet_candidate is candidate
        assert before == (candidate.seq, candidate.mark, candidate.token, candidate.source_decision_id)
        if not locked: await h.silence(4000)
    finally:
        if locked: h.call._handoff_lock.release()
    await settle(500)
    assert len(h.events('handoff_ready')) == 1
    await h.silence(100); assert len(h.events('handoff_ready')) == 1

async def test_queued_segment_and_unavailable_count_once(live528):
    h = live528; await guarded(h)
    reply(h, transfer='pending')
    for _ in range(3): await turn(h, 'Still waiting')
    await h.silence(600)
    assert len(speaker_decisions(h)) == 2 and h.call._quiet_candidate is None
    h.call._keypad.deps.jev = None
    for _ in range(3): await turn(h, 'Company sentence')
    await h.silence(600)
    assert h.call._keypad.policy_state.unavailable_run == 1
    assert h.call._quiet_candidate is None

async def test_non_guard_and_same_package_guard_still_immediate(live528):
    h = live528; reply(h); await turn(h, 'Automatic question')
    assert h.call._media_gate.output_state == 'allowed'
    reply(h, transfer='connected'); await turn(h, 'Announcement. Greeting.')
    assert len(h.events('handoff_ready')) == 1
    assert all('segment_seqs' not in d.policy_decision for d in speaker_decisions(h))

async def test_segment_trace_roundtrip_and_examples(live528, tmp_path):
    from waitless.trace import Trace
    from waitless.store import Store
    h = live528; await guarded(h); reply(h, transfer='none')
    await turn(h, 'Company clause'); await turn(h, '')
    empty = speaker_decisions(h)[-1]
    await h.silence(600); decision = speaker_decisions(h)[-1]
    db = Store(tmp_path / 'segment.sqlite')
    try:
        trace = Trace(db, h.clock)
        for d in (empty, decision): trace.event(d)
        got = trace.events(call_id=h.spec.call_id)
        assert [d.policy_decision for d in got] == [empty.policy_decision, decision.policy_decision]
        assert got[-1].jev_request == decision.jev_request
    finally: db.close()
    for name, event in [('segment', decision), ('empty', empty)]:
        print('V528_EXAMPLE ' + name + ' ' + json.dumps({
            'event_type': event.event_type, 'action': event.action,
            'policy_decision': event.policy_decision}))

async def test_segment_uses_config_and_cannot_seal_after_epoch_revocation(live528):
    h = live528; await guarded(h); reply(h, transfer='none')
    h.settings.engine.speaker.guard_segment_quiet_ms = 40
    await turn(h, 'Short segment'); await h.silence(20)
    assert len(speaker_decisions(h)) == 1
    await h.silence(20); assert len(speaker_decisions(h)) == 2
    await turn(h, 'Revoked segment'); await h.call._media_gate.invalidate_stream()
    await h.silence(600)
    assert len(speaker_decisions(h)) == 2

async def test_late_guard_moves_undecided_backlog_to_quiet_segment(live528):
    h = live528; wire = SplitWire(); wire.answer['transfer']['choice'] = 'pending'
    h.call._keypad.deps.jev = wire
    await turn(h, 'Old announcement')
    for n in range(8): await turn(h, f'Queued clause {n}')
    wire.release['main'].set(); wire.release['transfer'].set(); await settle(500)
    assert h.call._keypad.policy_state.guard
    # No post-guard HTTP request before quiet, even for the old backlog.
    assert h.call._keypad._last_decided_seq == 1
    assert not speaker_decisions(h)
    reply(h, transfer='none'); await h.silence(600)
    assert [d.action['turn_seq'] for d in speaker_decisions(h)] == [9]
    assert speaker_decisions(h)[-1].policy_decision['segment_seqs'] == [4, 5, 6, 7, 8, 9]

async def test_human_sentence_over_six_finals_handoffs_only_after_quiet(live528):
    h = live528; await guarded(h); reply(h, transfer='connected')
    for part in ['Thank you', 'for holding', 'I am sorry', 'our specialists',
                 'are still busy', 'instead of waiting', 'would you like', 'a callback']:
        await turn(h, part); await h.speech(20)
        assert not h.events('handoff_ready') and len(speaker_decisions(h)) == 1
    await h.silence(600); await settle(500)
    assert len(h.events('handoff_ready')) == 1
    assert speaker_decisions(h)[-1].policy_decision['segment_seqs'] == [4,5,6,7,8,9]

async def test_six_segment_rows_survive_key_history_and_transfer_truncation(live528):
    from fakes import FakeJev
    from lane_v528a.test_keypad_policy import script
    h = live528; h.call._keypad.deps.jev = FakeJev(default=script(key='*'))
    await turn(h, 'Automated menu'); await guarded(h)
    reply(h, transfer='none')
    for n in range(6): await turn(h, f'Clause {n}. Final part {n}.')
    await h.silence(600)
    d = speaker_decisions(h)[-1]
    assert d.policy_decision['segment_seqs'] == [3,4,5,6,7,8]
    assert len(d.jev_request['state']['transcript']['lines']) == 8
    for name in ('state', 'transfer_state'):
        lines = d.jev_request[name]['transcript']['lines']; assert len(lines) == 8
        for n in range(6): assert any(f'Clause {n}.' in line['text'] for line in lines)
