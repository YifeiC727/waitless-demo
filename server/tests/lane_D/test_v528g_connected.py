"""§15: configured connected threshold through real LiveCall and policy wiring."""
import pytest
import yaml
from pydantic import ValidationError
from waitless import models as m
from waitless.call.speaker_policy import SpeakerPolicyState, classify
from lane_v528a.test_policy import answers, config
from lane_D.test_v528d_requests import SplitWire, start
from lane_D.test_v528e_segments import guarded
from call_engine._support import settle
from lane_v528c2.test_wiring import (live528, reply, turn, speaker_decisions,
    agent_call, settings, spec, engine, compiled, line_number, store, trace)
from lane_v528a.test_config import ROOT


@pytest.mark.parametrize('guard', [False, True])
@pytest.mark.parametrize('confidence', [.39, .40, .41])
async def test_connected_boundary_same_package_and_quiet_segment(live528, guard, confidence):
    h = live528
    if guard:
        await guarded(h)
    reply(h, transfer='connected', tc=confidence)
    await turn(h, 'Representative greeting')
    if guard:
        assert not h.events('handoff_ready')
        assert len(speaker_decisions(h)) == 1
        await h.silence(600)
        await settle(500)
    expected = confidence >= .4
    assert h.call._keypad.policy_state.guard == (guard or expected)
    assert len(h.events('handoff_ready')) == int(expected)
    d = speaker_decisions(h)[-1]
    assert d.policy_decision['speaker_class'] == (
        'transfer_connected' if expected else 'guard_wait' if guard else 'automated')
    if expected:
        assert h.call.status().handoff.reason_kind == 'transfer_connected'
        assert h.call._media_gate.output_state == 'handoff'
        assert not h.call._keypad.queued
        assert not h.events('dtmf_sent')
    if guard:
        assert d.policy_decision['segment_seqs'] == [2]
        assert d.policy_decision['segment_trigger'] == 'quiet'
    else:
        assert 'segment_seqs' not in d.policy_decision


async def test_pending_point69_does_not_set_guard(live528):
    h = live528; reply(h, transfer='pending', tc=.69)
    await turn(h)
    assert not h.call._keypad.policy_state.guard
    assert not h.call._keypad.queued and not h.events('handoff_ready')
    assert h.call._media_gate.output_state == 'allowed'


@pytest.mark.parametrize('change', ['final', 'epoch'])
async def test_stale_connected_point5_only_tightens_guard(live528, change):
    h = live528; wire = SplitWire()
    wire.answer['transfer'].update(choice='connected', confidence=.5, probabilities={'connected':.5})
    await start(h, wire)
    if change == 'final':
        await turn(h, '')
    else:
        await h.call._media_gate.invalidate_stream()
    wire.release['main'].set(); wire.release['transfer'].set(); await settle(500)
    state = h.call._keypad.policy_state
    assert state.guard and state.guard_since_seq == 1
    assert state.guard_evidence['confidence'] == .5
    assert h.call._media_gate.output_state == 'closed'
    assert h.call._keypad._latest_epoch == h.call._media_gate.output_epoch
    assert not h.events('handoff_ready') and not h.events('dtmf_sent')


@pytest.mark.parametrize('guard', [False, True])
async def test_r1_precedes_low_confidence_connected_g1(live528, guard):
    h = live528
    if guard: await guarded(h)
    reply(h, who='live_human', confidence=.7, transfer='connected', tc=.4)
    await turn(h)
    if guard:
        assert not h.events('handoff_ready')
        await h.silence(600); await settle(500)
    assert h.call._keypad.policy_state.guard
    assert h.call.status().handoff.reason_kind == 'live_human'
    assert len(h.events('handoff_ready')) == 1


def test_connected_threshold_reads_engine_and_missing_config_errors():
    c = config(); c['engine'].connected_handoff_min_confidence = .6
    for confidence, expected in [(.59, False), (.60, True)]:
        v, s = classify(answers(transfer='connected', tc=confidence),
            state=SpeakerPolicyState(), config=c)
        assert s.guard == expected
        assert (v.handoff_kind == 'transfer_connected') == expected
    del c['engine'].connected_handoff_min_confidence
    with pytest.raises(AttributeError):
        classify(answers(transfer='connected', tc=.5), state=SpeakerPolicyState(), config=c)


MISSING = object()
@pytest.mark.parametrize('value', [MISSING, None, True, False, '0.4', 0, 1, -1,
    0., 1., -.1, 1.1, float('nan'), float('inf'), float('-inf'), [], {}])
def test_connected_threshold_strict_reject_matrix(value):
    data = yaml.safe_load((ROOT / 'server/config/engine.yaml').read_text())
    if value is MISSING: data['speaker'].pop('connected_handoff_min_confidence', None)
    else: data['speaker']['connected_handoff_min_confidence'] = value
    with pytest.raises(ValidationError): m.EngineConfig.model_validate(data)


@pytest.mark.parametrize('value', [.001, .4, .999])
def test_connected_threshold_accepts_finite_float(value):
    data = yaml.safe_load((ROOT / 'server/config/engine.yaml').read_text())
    data['speaker']['connected_handoff_min_confidence'] = value
    assert m.EngineConfig.model_validate(data).speaker.connected_handoff_min_confidence == value


def test_connected_threshold_shipped_and_extra_forbidden():
    data = yaml.safe_load((ROOT / 'server/config/engine.yaml').read_text())
    assert data['speaker']['connected_handoff_min_confidence'] == .4
    data['speaker']['connected_handoff_min_confidence_extra'] = .4
    with pytest.raises(ValidationError): m.EngineConfig.model_validate(data)
