"""Streaming Goertzel keypad detector (AC-79/80), for PCM16 little endian."""
import math
import struct

__all__ = ["DTMF_COLS_HZ", "DTMF_KEYS", "DTMF_ROWS_HZ", "GoertzelDetector"]
DTMF_ROWS_HZ: tuple[float, ...] = (697.0, 770.0, 852.0, 941.0)
DTMF_COLS_HZ: tuple[float, ...] = (1209.0, 1336.0, 1477.0)
DTMF_KEYS: tuple[tuple[str, ...], ...] = (("1", "2", "3"), ("4", "5", "6"), ("7", "8", "9"), ("*", "0", "#"))


class GoertzelDetector:
    def __init__(self, *, sample_rate: int, window_ms: int = 20, stable_ms: int = 40) -> None:
        frequencies = DTMF_ROWS_HZ + DTMF_COLS_HZ + (1633.0,)
        if sample_rate <= 2 * max(frequencies) or window_ms <= 0 or stable_ms <= 0:
            raise ValueError("Invalid detector sample rate or timing")
        self._samples = round(sample_rate * window_ms / 1000)
        if self._samples < 2:
            raise ValueError("Detector window needs at least two samples")
        self._coefficients = tuple(2 * math.cos(2 * math.pi * f / sample_rate) for f in frequencies)
        self._stable_windows = max(2, math.ceil(stable_ms / window_ms))
        self.reset()

    def _classify(self, pcm16: bytes) -> str | None:
        samples = struct.unpack(f'<{self._samples}h', pcm16)
        mean = sum(samples) / self._samples
        centered = [s - mean for s in samples]
        total = sum(s * s for s in centered)
        # Reject very low levels before evaluating relative spectral measures.
        if total / self._samples < (32768 * 10 ** (-45 / 20)) ** 2:
            return None
        powers = []
        for coefficient in self._coefficients:
            previous = earlier = 0.0
            for sample in centered:
                current = sample + coefficient * previous - earlier
                earlier, previous = previous, current
            powers.append(max(0.0, previous * previous + earlier * earlier - coefficient * previous * earlier))
        row = max(range(4), key=lambda i: powers[i])
        col = max(range(4, 8), key=lambda i: powers[i])
        low, high = powers[row], powers[col]
        # Each group must have a single peak at least 6 dB above its runners up.
        if low < 4 * max(powers[i] for i in range(4) if i != row):
            return None
        if high < 4 * max(powers[i] for i in range(4, 8) if i != col):
            return None
        # Both peaks must dominate every other measured frequency, including
        # the opposite group's runners up and the fourth (1633 Hz) column.
        # 3 dB leaves margin for weak pure tones at the allowed twist limits.
        other = max(powers[i] for i in range(8) if i not in (row, col))
        if min(low, high) < 10 ** (3 / 10) * other:
            return None
        # Two sinusoids must explain at least 75% of the window's energy.
        if 2 * (low + high) / (self._samples * total) < 0.75:
            return None
        # Normal and reverse twist: high group between -8 and +4 dB of low.
        # A Hann window reduces finite-window leakage in the twist estimate.
        # Resolve dB to one decimal place: codec quantization must not turn
        # a pure tone exactly on a nominal boundary into a release window.
        twist_powers = []
        for peak in (row, col):
            previous = earlier = 0.0
            coefficient = self._coefficients[peak]
            for n, sample in enumerate(centered):
                weight = 0.5 - 0.5 * math.cos(2 * math.pi * n / self._samples)
                current = sample * weight + coefficient * previous - earlier
                earlier, previous = previous, current
            twist_powers.append(previous * previous + earlier * earlier
                                - coefficient * previous * earlier)
        if min(twist_powers) <= 0:
            return None
        twist_db = round(10 * math.log10(twist_powers[1] / twist_powers[0]), 1)
        if not -8 <= twist_db <= 4:
            return None
        return DTMF_KEYS[row][col - 4] if col < 7 else None

    def feed(self, pcm16: bytes) -> list[str]:
        """Buffer arbitrary byte chunks; emit each stable press once until release."""
        self._buffer.extend(pcm16)
        result = []
        size = self._samples * 2
        while len(self._buffer) >= size:
            digit = self._classify(self._buffer[:size])
            del self._buffer[:size]
            if digit != self._candidate:
                self._candidate, self._count = digit, 0
            self._count += 1
            if digit is None:
                self._emitted = None
            elif self._count >= self._stable_windows and digit != self._emitted:
                result.append(digit)
                self._emitted = digit
        return result

    def reset(self) -> None:
        self._buffer = bytearray()
        self._candidate: str | None = None
        self._emitted: str | None = None
        self._count = 0
