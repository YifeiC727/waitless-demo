"""Agent DTMF tails reach the line without being counted as Sonic speech."""
import pytest
from lane_D.agent_support import *
from call_engine._support import audio_ms, settle
from pipecat.frames.frames import OutputAudioRawFrame


@pytest.mark.parametrize('phase,digits', [('ivr', '1'), ('live_rep', '12#')])
async def test_dtmf_tail_reaches_line_without_guard_events(agent_call, phase, digits):
    h = agent_call
    await h.boot()
    await h.tool('update_status', {'phase': phase, 'status_line': 'Listening'})
    assert (await h.tool('press_keys', {'digits': digits}))['ok']
    await settle(200)
    out = h.transport.output()
    assert ''.join(f.to_string() for f in out.dtmf()) == digits
    audio = [f for f in out.frames if isinstance(f, OutputAudioRawFrame)]
    assert audio_ms(audio) == h.settings.engine.dtmf.tail_silence_ms
    assert all(not any(f.audio) for f in audio)
    assert h.call._monitor.audio_serial == 0 and h.call._monitor.last_audio_ms is None
    await h.tool('request_handoff', {'reason': 'Verify'})
    assert (await h.tool('press_keys', {'digits': digits}))['error'] == 'line_in_handoff'
    assert len(out.dtmf()) == 1
    h.assert_trace_contract()
