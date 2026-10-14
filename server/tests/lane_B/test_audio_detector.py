"""Independent AC-80 criteria and streaming boundary regression checks."""
import math
import struct

import pytest

from waitless.company.dtmf_detect import GoertzelDetector

RATE = 8000


def signal(freqs, ms=100, gains=None):
    gains = gains or [1] * len(freqs)
    return b''.join(struct.pack('<h', round(1800 * sum(
        gain * math.sin(2 * math.pi * freq * n / RATE)
        for freq, gain in zip(freqs, gains)
    ))) for n in range(RATE * ms // 1000))


@pytest.mark.parametrize('freqs,gains', [
    ([697], None), ([1209], None),
    ([697, 770, 1209], None), ([697, 1209, 1336], None),
    ([697, 1209], [1, 5]), ([697, 1209], [5, 1]),
    ([697, 1209, 400, 2000], [1, 1, 2, 2]),
    ([697, 1633], None),  # fourth column is measured, but not a phone keypad key
])
def test_reject_missing_peak_competing_peak_twist_and_impurity(freqs, gains):
    assert GoertzelDetector(sample_rate=RATE).feed(signal(freqs, gains=gains)) == []


def test_stability_repetition_odd_chunks_and_reset():
    tone = signal([697, 1209], 20)
    detector = GoertzelDetector(sample_rate=RATE)
    assert detector.feed(tone) == []
    assert detector.feed(tone) == ['1']
    assert detector.feed(tone * 5) == []
    assert detector.feed(bytes(len(tone)) + tone * 2) == ['1']
    detector.reset()
    assert detector.feed(tone) == []
    detector.reset()  # discard incomplete candidates and buffered bytes
    data = tone * 5 + bytes(len(tone)) + tone * 5
    outputs = []
    for offset in range(0, len(data), 37):
        outputs.extend(detector.feed(data[offset:offset + 37]))
    assert outputs == ['1', '1']


def test_candidate_change_requires_two_windows():
    detector = GoertzelDetector(sample_rate=RATE)
    assert detector.feed(signal([697, 1209], 20)) == []
    assert detector.feed(signal([770, 1336], 20)) == []
    assert detector.feed(signal([770, 1336], 20)) == ['5']
