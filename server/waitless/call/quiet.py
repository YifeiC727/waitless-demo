"""Synchronous inbound PCM energy accounting; never an identity signal."""

from dataclasses import dataclass
import math
import struct

from waitless.interfaces import Clock


@dataclass(frozen=True)
class QuietMark:
    seq: int
    mark_audio_ms: int
    mark_wall_ms: int


class QuietMeter:
    """Thresholds come from the caller's validated engine/VAD configuration.

    Register ``observe`` with MediaGate.inbound_observers. Save the mark returned
    by on_company_final before awaiting a verdict; qualifies rechecks that same
    mark against current audio, residue and arrival freshness. Window observers
    run synchronously and may schedule, but must not await or block.
    """

    def __init__(self, *, frame_ms: int, threshold_dbfs: float,
                 quiet_max_gap_ms: int, uncertain_quiet_ms: int, clock: Clock):
        self.frame_ms = frame_ms
        self.threshold_dbfs = threshold_dbfs
        self.quiet_max_gap_ms = quiet_max_gap_ms
        self.uncertain_quiet_ms = uncertain_quiet_ms
        self.clock = clock
        self.audio_total_ms = 0
        self.reset_audio_ms = 0
        self.reset_wall_ms = clock.monotonic_ms()
        self.last_arrival_ms = None
        self.voiced_serial = 0
        self.residue = b''
        self.marks: dict[int, QuietMark] = {}
        self.window_observers = []
        self._format = None

    @staticmethod
    def _dbfs(audio: bytes) -> float:
        if not audio:
            return -math.inf
        squares = sum(sample * sample for (sample,) in struct.iter_unpack('<h', audio))
        if not squares:
            return -math.inf
        return 20 * math.log10(math.sqrt(squares / (len(audio) // 2)) / 32768)

    def observe(self, audio: bytes, sample_rate: int, num_channels: int, arrival_ms: int) -> None:
        window_samples, remainder = divmod(sample_rate * self.frame_ms, 1000)
        if sample_rate <= 0 or num_channels <= 0 or window_samples <= 0 or remainder:
            raise ValueError('PCM format must yield a positive integral window')
        if len(audio) % 2:
            raise ValueError('PCM16 audio must contain complete samples')
        fmt = (sample_rate, num_channels)
        if ((self._format is not None and self._format != fmt)
                or (self.last_arrival_ms is not None
                    and arrival_ms - self.last_arrival_ms > self.quiet_max_gap_ms)):
            self.residue = b''
            self.reset_audio_ms = self.audio_total_ms
            self.reset_wall_ms = arrival_ms
        self._format = fmt
        self.last_arrival_ms = arrival_ms
        data = self.residue + audio
        window_bytes = window_samples * num_channels * 2
        complete = len(data) // window_bytes * window_bytes
        self.residue = data[complete:]
        for offset in range(0, complete, window_bytes):
            self.audio_total_ms += self.frame_ms
            if self._dbfs(data[offset:offset + window_bytes]) >= self.threshold_dbfs:
                self.voiced_serial += 1
                self.reset_audio_ms = self.audio_total_ms
                self.reset_wall_ms = arrival_ms
            for observer in tuple(self.window_observers):
                observer()

    def on_company_final(self, seq: int) -> QuietMark:
        mark = QuietMark(seq, self.audio_total_ms + (self.frame_ms if self.residue else 0),
                         self.clock.monotonic_ms())
        self.marks[seq] = mark
        return mark

    def qualifies(self, mark: QuietMark, now_ms: int) -> bool:
        if self.last_arrival_ms is None:
            return False
        start_audio = max(self.reset_audio_ms, mark.mark_audio_ms)
        start_wall = max(self.reset_wall_ms, mark.mark_wall_ms)
        return (self.audio_total_ms - start_audio >= self.uncertain_quiet_ms
                and self.last_arrival_ms - start_wall >= self.uncertain_quiet_ms
                and 0 <= now_ms - self.last_arrival_ms <= self.quiet_max_gap_ms
                and self._dbfs(self.residue) < self.threshold_dbfs)
