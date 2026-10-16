"""U95: outbound jitter must not fragment speech or discard its tail."""
from types import SimpleNamespace

import pytest

from waitless.call import listen

SILENCE = bytes(640)
VOICE = b'\x10\x00' * 320


def mixer(**settings):
    return listen.ListenMixer(sample_rate=16000, frame_bytes=640,
                              max_lag_ms=1000, **settings)


def test_u95_waits_for_prebuffer():
    mix = mixer(prebuffer_ms=120)
    mix.add_line_out(VOICE * 2)
    assert mix.add_line_in(SILENCE) == [SILENCE]
    mix.add_line_out(VOICE * 4)
    assert mix.add_line_in(SILENCE) == [VOICE]


def test_u95_no_holes_with_bursty_supply():
    mix = mixer(prebuffer_ms=120)
    mix.add_line_out(VOICE * 6)
    for index in range(1, 51):
        if index % 2 == 0 and index not in (10, 12):
            mix.add_line_out(VOICE * 2)
        if index == 13:
            mix.add_line_out(VOICE * 4)
        assert mix.add_line_in(SILENCE) == [VOICE], index


def test_u95_tail_drains_without_loss():
    mix = mixer(prebuffer_ms=120)
    mix.add_line_out(VOICE * 10)
    frames = [mix.add_line_in(SILENCE)[0] for _ in range(30)]
    assert sum(frame.count(b'\x10\x00') for frame in frames) == 3200


def test_u95_mid_speech_underrun_rebuffers_instead_of_zero_fill():
    mix = mixer(prebuffer_ms=120)
    mix.add_line_out(VOICE * 6)
    assert mix.add_line_in(SILENCE) == [VOICE]
    mix.add_line_out(b'\x10\x00' * 150)
    for _ in range(5):
        assert mix.add_line_in(SILENCE) == [VOICE]
    assert mix.add_line_in(SILENCE) == [SILENCE]
    assert mix.out_buffered_ms == 9.375
    mix.add_line_out(b'\x10\x00' * 1770)
    assert mix.add_line_in(SILENCE) == [VOICE]


def test_u95_zero_prebuffer_keeps_old_behavior():
    mix = mixer()
    mix.add_line_out(VOICE * 2)
    assert mix.add_line_in(SILENCE) == [VOICE]


def test_u95_hub_uses_prebuffer_constant():
    hub = listen.ListenHub(media_gate=SimpleNamespace(in_handoff=False), sample_rate=16000)
    assert hub._mixer.prebuffer_bytes == 3840
    assert listen.LISTEN_OUT_PREBUFFER_MS == 120


def test_u95_negative_prebuffer_is_rejected():
    with pytest.raises(ValueError):
        mixer(prebuffer_ms=-1)


def test_u95_clear_out_rebuffers():
    mix = mixer(prebuffer_ms=120)
    mix.add_line_out(VOICE * 6)
    assert mix.add_line_in(SILENCE) == [VOICE]
    mix.clear_out()
    mix.add_line_out(VOICE * 2)
    assert mix.add_line_in(SILENCE) == [SILENCE]
