"""F4: arrival-clock continuation with real LiveCall, Tap and MediaGate."""
import asyncio
import pytest
from pipecat.processors.frame_processor import FrameDirection
from waitless import models as m
from call_engine._support import settle
from lane_D.test_u119_gate import audio
from lane_D.test_v528d_requests import SplitWire
from lane_v528c2.test_wiring import (live528, reply, turn, speaker_decisions,
    agent_call, settings, spec, engine, compiled, line_number, store, trace)


def owner(h):
    g, k = h.call._media_gate, h.call._keypad
    return g.company_seq, g.output_epoch, k._latest_seq, k._last_decided_seq, k._latest_epoch

async def final(h, text, stamp=0):
    await h.call._tap.on_provider_text(m.ProviderTextEvent(role='USER', stage='FINAL',
        text=text, stop_reason='END_TURN', t_ms=stamp))
    await settle(300)

async def allowed(h, text='Company question', stamp=0):
    reply(h); await final(h, text, stamp)
    assert h.call._media_gate.output_state == 'allowed'

@pytest.mark.parametrize('gap,continues', [(2999, True), (3000, True), (3001, False)])
async def test_f4_boundary_and_trace_owner_no_jev_no_timers(live528, gap, continues):
    h = live528; await allowed(h); before = owner(h); state = h.call._keypad.policy_state
    await h.clock.advance(gap); await final(h, '')
    d = speaker_decisions(h)[-1]; p = d.policy_decision
    assert p['speaker_class'] == 'empty' and p['no_jev'] is True
    assert d.action['turn_seq'] == 2 and d.jev_response == {'answers': {}}
    assert d.jev_request['questions'] == {} and d.jev_request['state']['transcript']['lines'] == []
    assert h.call._keypad.policy_state == state and not h.call._tap._keypad_tasks
    if continues:
        assert owner(h) == before and h.call._media_gate.output_state == 'allowed'
        assert p['action'] == 'allow' and p['reason_code'] == 'speaker_empty_continuation'
        assert p['permit_owner_seq'] == 1 and p['continuation_gap_ms'] == gap
        assert h.call._quiet_candidate is None and h.call._stuck is None
        assert 2 not in h.call._quiet.marks
    else:
        assert h.call._media_gate.output_state == 'closed' and owner(h) != before
        assert p['reason_code'] == 'speaker_empty_transcript'
        assert h.call._quiet_candidate is not None and h.call._stuck is not None

async def test_f4_consecutive_empty_and_placeholder_do_not_extend_age(live528):
    h = live528; await allowed(h)
    for step, text in [(1000, ''), (1000, 'Hello [name]'), (1000, '[_ahhh]')]:
        await h.clock.advance(step); await final(h, text)
        assert speaker_decisions(h)[-1].policy_decision['reason_code'] == 'speaker_empty_continuation'
        assert h.call._media_gate.company_seq == 1
    await h.clock.advance(1); await final(h, '[agent name]')
    assert speaker_decisions(h)[-1].policy_decision['reason_code'] == 'speaker_placeholder_transcript'
    assert h.call._media_gate.output_state == 'closed' and h.call._media_gate.company_seq == 5

@pytest.mark.parametrize('first_stamp,empty_stamp', [(900000, 1), (1, 900000)])
async def test_f4_provider_timestamps_never_control_age(live528, first_stamp, empty_stamp):
    h = live528; await allowed(h, stamp=first_stamp)
    await h.clock.advance(3000); await final(h, ' ', empty_stamp)
    assert speaker_decisions(h)[-1].policy_decision['continuation_gap_ms'] == 3000
    assert h.call._media_gate.output_state == 'allowed'
    await h.clock.advance(1); await final(h, '', first_stamp)
    assert h.call._media_gate.output_state == 'closed'

async def test_f4_bound_frames_and_unspoken_final_survive_continuation(live528):
    h = live528; await allowed(h); g = h.call._media_gate
    g.bind_response((1, 1)); before = owner(h)
    ev = m.ProviderTextEvent(role='ASSISTANT', stage='FINAL', text='Existing answer',
        response=1, stop_reason='END_TURN', t_ms=0)
    object.__setattr__(ev, 'response_identity', (1, 1))
    await h.call._tap.on_provider_text(ev)
    await h.call._tap.on_response_end(1, h.clock.monotonic_ms())
    assert not h.events('waitless_utterance')
    assert (1, 1) in h.call._tap._unspoken
    await h.clock.advance(1825); await final(h, '')
    frame = audio(); frame._waitless_output_epoch = before[1]
    count = len(h.transport.output().tts_audio())
    await g.outbound.process_frame(frame, FrameDirection.DOWNSTREAM); await settle(100)
    assert owner(h) == before and len(h.transport.output().tts_audio()) == count + 1
    assert g.response_audible((1, 1))
    assert [e.transcript for e in h.events('waitless_utterance')] == ['Existing answer']

@pytest.mark.parametrize('mode,kind', [('r1', 'live_human'), ('g1', 'transfer_connected')])
async def test_f4_next_semantic_human_or_same_package_g1_revokes_before_await(live528, mode, kind):
    h = live528; await allowed(h); await final(h, '')
    g = h.call._media_gate; g.bind_response((1, 1)); epoch = g.output_epoch
    entered, release = asyncio.Event(), asyncio.Event(); original = g._cleaner
    async def clean():
        entered.set(); await release.wait()
        if original is not None: await original()
    g.set_output_cleaner(clean)
    reply(h, who='live_human' if mode == 'r1' else 'virtual_agent', confidence=.9,
        transfer='none' if mode == 'r1' else 'connected')
    task = asyncio.create_task(final(h, 'Representative greeting'))
    try:
        await entered.wait()
        assert g.output_state == 'pending' and g.output_epoch > epoch and g.company_seq == 3
        assert not g.response_audible((1, 1)) and not h.events('handoff_ready')
    finally:
        release.set(); await task
    assert h.call.status().handoff.reason_kind == kind
    assert len(h.events('handoff_ready')) == 1

@pytest.mark.parametrize('transfer', ['none', 'pending'])
async def test_f4_old_pair_returning_during_continuation_cannot_reopen(live528, transfer):
    h = live528; await allowed(h, 'Old question'); k = h.call._keypad; g = h.call._media_gate
    wire = SplitWire(); wire.answer['transfer']['choice'] = transfer; k.deps.jev = wire
    old = asyncio.create_task(k._decide(1)); await settle(100)
    assert set(wire.calls) == {'main', 'transfer'}
    await allowed(h, 'Current question'); before = owner(h)
    await final(h, '')
    assert speaker_decisions(h)[-1].policy_decision['reason_code'] == 'speaker_empty_continuation'
    assert owner(h) == before
    g.revoke_closed()
    wire.release['main'].set(); wire.release['transfer'].set(); await old
    assert g.output_state == 'closed' and not h.events('handoff_ready')
    assert k.policy_state.guard == (transfer == 'pending')

@pytest.mark.parametrize('mode', ['hold', 'pending', 'closed', 'guard'])
async def test_f4_disallowed_gate_and_guard_never_continue(live528, mode):
    h = live528
    if mode == 'pending':
        wire = SplitWire(); h.call._keypad.deps.jev = wire
        await final(h, 'Pending question')
    else:
        reply(h, who='hold' if mode == 'hold' else 'virtual_agent',
            ph=.2 if mode == 'closed' else .01, transfer='pending' if mode == 'guard' else 'none')
        await turn(h)
    await final(h, '')
    assert speaker_decisions(h)[-1].policy_decision['reason_code'] == 'speaker_empty_transcript'
    assert h.call._media_gate.output_state != 'allowed' and h.call._media_gate.company_seq == 2
    if mode == 'pending':
        wire.release['main'].set(); wire.release['transfer'].set(); await settle(300)
        assert h.call._media_gate.output_state != 'allowed'

@pytest.mark.parametrize('mismatch', ['latest_seq', 'last_decided_seq', 'latest_epoch', 'no_semantic_arrival'])
async def test_f4_all_ownership_equalities_and_previous_arrival_required(live528, mismatch):
    h = live528; await allowed(h); k = h.call._keypad
    if mismatch == 'no_semantic_arrival': h.call._tap._last_semantic_final_ms = None
    else: setattr(k, '_' + mismatch, getattr(k, '_' + mismatch) + 1)
    await final(h, '')
    assert h.call._media_gate.output_state == 'closed'
    assert speaker_decisions(h)[-1].policy_decision['reason_code'] == 'speaker_empty_transcript'

async def test_f4_window_reads_config_and_missing_field_is_required(live528):
    h = live528; config = h.settings.engine.speaker
    assert getattr(config, 'empty_continuation_ms', None) == 3000
    config.empty_continuation_ms = 40
    await allowed(h); await h.clock.advance(40); await final(h, '')
    assert h.call._media_gate.output_state == 'allowed'
    await h.clock.advance(1); await final(h, '')
    assert h.call._media_gate.output_state == 'closed'
