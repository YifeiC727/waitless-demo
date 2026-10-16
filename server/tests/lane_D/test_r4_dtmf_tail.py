"""Jev DTMF tails reach the line without being counted as Sonic speech."""
import pytest
from lane_D.agent_support import *
from call_engine._support import audio_ms, settle
from pipecat.frames.frames import OutputAudioRawFrame
from fakes import FakeJev, jev_choice


@pytest.mark.parametrize('phase,choice,digits', [('ivr', '1', '1'), ('live_rep', 'phone_number#', '3125550142#')])
async def test_dtmf_tail_reaches_line_without_guard_events(agent_call, phase, choice, digits):
    h = agent_call
    h.spec.compiled.allowed_fields['phone_number'] = '(312) 555-0142'
    h.deps.jev = FakeJev(default={'keypad': jev_choice(choice)})
    await h.boot()
    await h.tool('update_status', {'phase': phase, 'status_line': 'Listening'})
    await h.sonic.emit_user_text('Enter the requested keys.')
    await settle(200)
    out = h.transport.output()
    assert ''.join(f.to_string() for f in out.dtmf()) == digits
    audio = [f for f in out.frames if isinstance(f, OutputAudioRawFrame)]
    assert audio_ms(audio) == h.settings.engine.dtmf.tail_silence_ms
    assert all(not any(f.audio) for f in audio)
    assert h.call._monitor.audio_serial == 0 and h.call._monitor.last_audio_ms is None
    await h.tool('request_handoff', {'reason': 'Verify'})
    await h.clock.advance(h.settings.engine.handoff.announce_idle_ms + 10)
    await settle(200)
    await h.call._keypad.on_company_turn(2)
    await settle(200)
    assert len(h.deps.jev.calls) == 1
    assert len(out.dtmf()) == 1
    h.assert_trace_contract()
