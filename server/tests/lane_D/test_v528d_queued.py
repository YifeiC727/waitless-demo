"""F3: real call/quiet ownership and synchronous transfer-state advancement."""
import asyncio
from dataclasses import replace
from pathlib import Path
import pytest
import yaml
from call_engine._support import settle
from lane_D.test_v528d_requests import SplitWire, start
from lane_v528c2.test_wiring import (live528, reply, turn, speaker_decisions,
    agent_call, settings, spec, engine, compiled, line_number, store, trace)
from lane_v528a.test_policy import answers


def advance(h, seq, *, choice='pending', confidence=.9, stale=True):
    k = h.call._keypad
    method = getattr(k, '_advance_policy', None)
    assert callable(method), 'one synchronous policy advancement function required'
    assert not asyncio.iscoroutinefunction(method)
    cfg = dict(keypad=k.config, speaker=k.speaker_config, transfer=k.transfer_config,
        engine=h.settings.engine.speaker, seq=seq, evidence='Company final')
    return method(answers(transfer=choice, tc=confidence), config=cfg, stale=stale)

async def pending(h, **kwargs):
    reply(h, transfer='pending', **kwargs)
    await turn(h)

async def unqueued_guard(h):
    await pending(h)
    reply(h, transfer='pending', tc=.69)
    await turn(h)
    assert h.call._quiet_candidate is not None

@pytest.mark.parametrize('entry', ['first', 'g0', 'hold', 'unavailable', 'empty', 'placeholder'])
async def test_f3_all_guard_candidate_entries_respect_queued(live528, entry):
    h = live528
    await pending(h, who='hold' if entry == 'hold' else 'virtual_agent')
    if entry == 'g0': await pending(h)
    elif entry == 'unavailable':
        h.call._keypad.deps.jev = None; await turn(h)
    elif entry in ('empty', 'placeholder'):
        await turn(h, '' if entry == 'empty' else 'Hello [name]')
    assert h.call._quiet_candidate is None
    assert h.call._stuck is None
    assert h.call._keypad.policy_state.guard
    assert getattr(h.call._keypad, 'queued', None) is True
    assert h.call._media_gate.output_state in ('closed', 'hold')

async def test_f3_last_transfer_and_queued_ready_before_notify_guard(live528):
    h = live528; k = h.call._keypad; observed = []; original = k.on_guard
    def observe():
        observed.append((getattr(k.policy_state, 'last_transfer', None),
            getattr(k, 'queued', None), h.call._quiet_candidate))
        original()
        observed.append((getattr(k.policy_state, 'last_transfer', None),
            getattr(k, 'queued', None), h.call._quiet_candidate))
    k.on_guard = observe
    await pending(h)
    assert observed == [((1, 'pending', .9), True, None), ((1, 'pending', .9), True, None)]

async def test_f3_stale_valid_pending_suppresses_empty_candidate_and_closes(live528):
    h = live528; wire = SplitWire(); wire.answer['transfer']['choice'] = 'pending'
    await start(h, wire)
    await h.sonic.emit_user_text(''); await settle(100)
    assert h.call._quiet_candidate is not None
    wire.release['main'].set(); wire.release['transfer'].set(); await settle(300)
    assert h.call._quiet_candidate is None
    assert h.call._keypad.policy_state.last_transfer == (1, 'pending', .9)
    assert h.call._keypad.queued
    assert h.call._media_gate.output_state == 'closed'
    assert h.call._keypad._latest_epoch == h.call._media_gate.output_epoch

async def test_f3_pending_survives_unavailable_empty_placeholder_and_silence(live528):
    h = live528; await pending(h)
    h.call._keypad.deps.jev = None; await turn(h)
    await turn(h, ''); await turn(h, '[agent name]')
    assert h.call._quiet_candidate is None
    assert h.call._keypad.policy_state.last_transfer == (1, 'pending', .9)
    assert h.call._keypad.policy_state.unavailable_run == 1
    await h.silence(h.settings.engine.speaker.uncertain_quiet_ms)
    await h.clock.advance(h.settings.engine.speaker.uncertain_max_wait_ms)
    await settle(200)
    assert not h.events('handoff_ready') and h.call._stuck is None

@pytest.mark.parametrize('choice,confidence', [('pending', .69), ('none', .9), ('connected', .69)])
async def test_f3_new_result_restores_mark_or_connected_handoffs(live528, choice, confidence):
    h = live528; await pending(h)
    reply(h, transfer=choice, tc=confidence); await turn(h)
    c = h.call._quiet_candidate
    if choice == 'connected':
        assert c is None and len(h.events('handoff_ready')) == 1
        assert h.call.status().handoff.reason_kind == 'transfer_connected'
        assert h.call._keypad.policy_state.last_transfer == (2, choice, confidence)
        assert not h.call._keypad.queued
        return
    assert c is not None and c.seq == 2 and c.mark is h.call._quiet.marks[2]
    assert h.call._keypad.policy_state.last_transfer == (2, choice, confidence)
    assert h.call._keypad.queued is False
    assert c.source_decision_id == speaker_decisions(h)[-1].decision_id
    assert c.reason_code == 'speaker_quiet_in_guard'

async def test_f3_already_guarded_pending_changes_token_before_locked_fallback(live528):
    h = live528; await unqueued_guard(h)
    await h.call._handoff_lock.acquire()
    try:
        await h.silence(h.settings.engine.speaker.uncertain_quiet_ms)
        candidate = h.call._quiet_candidate
        assert candidate.inflight is not None
        token = h.call._quiet_token
        advance(h, 3)
        assert h.call._quiet_candidate is None and h.call._quiet_token != token
        assert h.call._keypad.policy_state.last_transfer == (3, 'pending', .9)
    finally: h.call._handoff_lock.release()
    await settle(300)
    assert not h.events('handoff_ready')

async def test_f3_late_old_pending_cannot_overwrite_newer_none(live528):
    h = live528; await pending(h)
    reply(h, transfer='none'); await turn(h)
    c = h.call._quiet_candidate
    advance(h, 1)
    assert h.call._keypad.policy_state.last_transfer == (2, 'none', .9)
    assert h.call._keypad.queued is False and h.call._quiet_candidate is c

async def test_f3_none_is_not_queued_and_threshold_comes_from_config(live528):
    h = live528; k = h.call._keypad
    assert getattr(k, 'queued', None) is False
    k.policy_state = replace(k.policy_state, guard=True)
    assert k.queued is False
    k.transfer_config['min_confidence'] = .8
    await pending(h)
    reply(h, transfer='pending', tc=.75); await turn(h)
    assert not k.queued and h.call._quiet_candidate is not None

async def test_f3_queued_unavailable_cap_still_hands_to_user(live528):
    h = live528; await pending(h)
    h.call._keypad.deps.jev = None
    for _ in range(h.settings.engine.speaker.unavailable_max_attempts): await turn(h)
    assert len(h.events('handoff_ready')) == 1
    assert h.call.status().handoff.reason_kind == 'needs_user'
    assert speaker_decisions(h)[-1].policy_decision['reason_code'] == 'speaker_unavailable_repeated'

async def test_f3_external_company_hard_limit_ends_queued_call(live528):
    h = live528; await pending(h)
    await h.silence(h.settings.engine.speaker.uncertain_quiet_ms)
    assert not h.events('handoff_ready')
    root = Path(__file__).resolve().parents[3]
    cap_ms = yaml.safe_load((root / 'company/bluefin-harbor-bank/scenarios/baseline.yaml').read_text())['max_call_seconds'] * 1000
    deadline = h.call._started_ms + cap_ms
    async def line_cutoff():
        await h.clock.sleep((deadline - h.clock.monotonic_ms()) / 1000)
        await h.call._on_line_disconnected(None, None)
    cutoff = asyncio.create_task(line_cutoff()); await settle(20)
    try:
        await h.clock.advance(deadline - h.clock.monotonic_ms() - 1)
        assert not h.call.ended
        await h.clock.advance(1); await cutoff
        assert h.call.ended and not h.events('handoff_ready')
        assert h.events('call_ended')[-1].action['reason'] == 'company_hangup'
    finally:
        if not cutoff.done(): cutoff.cancel()
        await asyncio.gather(cutoff, return_exceptions=True)

@pytest.mark.parametrize('side', ['main', 'transfer'])
async def test_f3_invalid_nonfinite_part_keeps_queue_and_trace_json_safe(live528, side):
    import json
    from types import SimpleNamespace
    from waitless import models as m
    h = live528; await pending(h)
    async def ask(state, questions):
        out = answers()
        target = 'speaker' if side == 'main' else 'transfer'
        out[target].confidence = float('nan')
        return m.JevResponse.model_construct(answers={key: value for key, value in out.items() if key in questions})
    h.call._keypad.deps.jev = SimpleNamespace(ask=ask)
    await turn(h)
    d = speaker_decisions(h)[-1]
    json.dumps(d.jev_response, allow_nan=False)
    assert d.jev_response['parts'][side]['status'] == 'invalid'
    assert d.jev_response['answers'] == {}
    assert h.call._keypad.policy_state.last_transfer == (1, 'pending', .9)
    assert h.call._quiet_candidate is None


# §13: guarded semantic FINALs require a quiet segment before a verdict.
_raw_turn = turn
async def turn(h, text='Company line'):
    from waitless.call.keypad import semantic_company_text
    guard = h.call._keypad.policy_state.guard
    before = len(speaker_decisions(h))
    await _raw_turn(h, text)
    if guard and semantic_company_text(text)[0].strip():
        assert len(speaker_decisions(h)) == before
        await h.silence(h.settings.engine.speaker.guard_segment_quiet_ms)
        await settle(300)
