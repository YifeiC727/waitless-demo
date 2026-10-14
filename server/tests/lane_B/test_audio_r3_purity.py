"""Both DTMF peaks must dominate all six remaining measured frequencies."""
import math
import struct

import pytest

from waitless.audio.codec import ulaw_decode, ulaw_encode
from waitless.company.dtmf_detect import DTMF_COLS_HZ, DTMF_KEYS, DTMF_ROWS_HZ, GoertzelDetector

RATE = 8000


def received_tone(frequencies, gains, milliseconds=100):
    pcm = b"".join(struct.pack("<h", round(6000 * sum(
        gain * math.sin(2 * math.pi * frequency * n / RATE)
        for frequency, gain in zip(frequencies, gains))))
        for n in range(RATE * milliseconds // 1000))
    return ulaw_decode(ulaw_encode(pcm))


def detect(pcm):
    detector = GoertzelDetector(sample_rate=RATE)
    return [key for offset in range(0, len(pcm), 320)
            for key in detector.feed(pcm[offset:offset + 320])]


@pytest.mark.parametrize("third,third_db,twist_db", [(852, -6.6, -7.5), (941, -7, -7.9)])
def test_reject_cross_group_competing_tone(third, third_db, twist_db):
    pcm = received_tone([697, 1209, third],
                        [1, 10 ** (twist_db / 20), 10 ** (third_db / 20)])
    assert detect(pcm) == []


@pytest.mark.parametrize("row", range(4))
@pytest.mark.parametrize("col", range(3))
@pytest.mark.parametrize("twist_db", [-8, 0, 4])
def test_pure_key_survives_twist_and_mulaw(row, col, twist_db):
    pcm = received_tone([DTMF_ROWS_HZ[row], DTMF_COLS_HZ[col]],
                        [1, 10 ** (twist_db / 20)])
    assert detect(pcm) == [DTMF_KEYS[row][col]]


@pytest.mark.parametrize("row", range(4))
@pytest.mark.parametrize("col", range(3))
@pytest.mark.parametrize("twist_db", [-8.2, 4.2])
def test_pure_key_outside_twist_limits_is_rejected(row, col, twist_db):
    pcm = received_tone([DTMF_ROWS_HZ[row], DTMF_COLS_HZ[col]],
                        [1, 10 ** (twist_db / 20)])
    assert detect(pcm) == []


def test_fourth_column_competes_with_both_key_peaks():
    assert detect(received_tone([697, 1209, 1633], [1, 1, 0.8])) == []
