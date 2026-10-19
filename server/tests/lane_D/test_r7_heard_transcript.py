"""Provider controls are protocol records; ordinary FINAL text is preserved."""
import pytest
from types import SimpleNamespace
from lane_D.agent_support import *
from waitless.call.live import TranscriptTap
from waitless import models as m


def tap_env(h):
    return TranscriptTap(deps=h.deps, spec=h.spec, toolbox=SimpleNamespace(phase='live_rep', note_speech=lambda: None),
        media_gate=SimpleNamespace(in_handoff=False))


def event(text, *, response=1, role='ASSISTANT'):
    return m.ProviderTextEvent(role=role, stage='FINAL', text=text, response=response,
        t_ms=2074, stop_reason='END_TURN')


@pytest.mark.parametrize('role', ['USER', 'ASSISTANT'])
@pytest.mark.parametrize('text', ['{ "interrupted" : true }', '\n {"interrupted":true}  '])
async def test_control_final_never_routes_even_without_guard(agent_call, role, text):
    tap = tap_env(agent_call)
    await tap.on_provider_text(event(text, role=role, response=None))
    assert not agent_call.events('waitless_utterance') and not agent_call.events('company_turn')
    await tap.on_provider_text(event('A real final.', response=None))
    assert [e.transcript for e in agent_call.events('waitless_utterance')] == ['A real final.']


@pytest.mark.parametrize('text', ['{"interrupted":false}', '{"interrupted":true,"other":1}', '{"interrupted":1}'])
async def test_only_exact_boolean_control_object_is_filtered(agent_call, text):
    await tap_env(agent_call).on_provider_text(event(text, response=None))
    assert [e.transcript for e in agent_call.events('waitless_utterance')] == [text]


@pytest.mark.parametrize('response', [None, 1])
async def test_legacy_final_without_guard_or_response_still_routes(agent_call, response):
    await tap_env(agent_call).on_provider_text(event('Legacy final.', response=response))
    assert [e.transcript for e in agent_call.events('waitless_utterance')] == ['Legacy final.']
