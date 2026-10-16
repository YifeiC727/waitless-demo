from lane_D.agent_support import *
from waitless.models import ProviderTextEvent

async def test_sonic_audio_reaches_line_immediately(agent_call):
    h = agent_call
    await h.boot()
    await h.tool('update_status', {'phase': 'ivr', 'status_line': 'Menu'})
    await h.sonic_says('Listening to the menu')
    assert h.transport.output().tts_audio()
    h.assert_trace_contract()

async def test_one_utterance_per_response(agent_call):
    h = agent_call
    await h.boot()
    listener = h.sonic._listeners[0]
    def ev(text, stop, stage='FINAL'):
        return ProviderTextEvent(role='ASSISTANT', stage=stage, text=text,
            response=1, stop_reason=stop, t_ms=h.clock.monotonic_ms())
    await listener(ev('Hello ', 'PARTIAL_TURN', 'SPECULATIVE'))
    await listener(ev('there.', 'END_TURN', 'SPECULATIVE'))
    await listener(ev('Hello ', 'PARTIAL_TURN'))
    assert not h.events('waitless_utterance')
    await listener(ev('there.', 'END_TURN'))
    assert [e.transcript for e in h.events('waitless_utterance')] == ['Hello there.']

async def test_event_text_is_not_a_company_turn(agent_call):
    h = agent_call
    await h.boot()
    await h.sonic.emit_user_text('[Waitless event] {"event":"handoff_ended"}')
    assert not h.events('company_turn')

async def test_company_turn_kind_follows_reported_phase(agent_call):
    h = agent_call
    await h.boot()
    await h.tool('update_status', {'phase': 'ivr', 'status_line': 'Menu'})
    await h.sonic.emit_user_text('Welcome.')
    await h.tool('update_status', {'phase': 'live_rep', 'status_line': 'Representative'})
    await h.sonic.emit_user_text('How can I help?')
    assert [e.action for e in h.events('company_turn')] == [{'kind': 'menu', 'seq': 1}, {'kind': 'speech', 'seq': 2}]
