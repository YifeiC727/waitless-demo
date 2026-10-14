"""Polly neural PCM prompts and deterministic, programmatic hold audio."""
import math
import struct
import threading
from typing import Any

__all__ = ["PromptCache", "hold_music", "synthesize_prompt"]


def synthesize_prompt(text: str, *, voice: str, polly_client: Any, sample_rate: int) -> bytes:
    """Return the complete Polly PCM stream; propagate service and stream errors."""
    response = polly_client.synthesize_speech(
        Engine="neural", VoiceId=voice, OutputFormat="pcm",
        SampleRate=str(sample_rate), Text=text,
    )
    stream = response["AudioStream"]
    try:
        return stream.read()
    finally:
        stream.close()


def hold_music(seconds: float, *, sample_rate: int) -> bytes:
    """Quiet, repeating musical notes as PCM16 LE, with smooth note envelopes."""
    if not math.isfinite(seconds) or seconds < 0 or sample_rate <= 0:
        raise ValueError("Hold duration must be nonnegative and sample rate positive")
    notes = (261.63, 329.63, 392.0, 329.63)
    samples = bytearray()
    for n in range(round(seconds * sample_rate)):
        t = n / sample_rate
        note = notes[int(t / 0.5) % len(notes)]
        phase = (t % 0.5) / 0.5
        envelope = min(1.0, phase * 20, (1 - phase) * 20)
        value = 0.1 * 32767 * envelope * (
            math.sin(2 * math.pi * note * t) + 0.25 * math.sin(4 * math.pi * note * t)
        )
        samples.extend(struct.pack('<h', round(value)))
    return bytes(samples)


class PromptCache:
    """In-memory cache scoped to one voice, sample rate, and Polly client."""
    def __init__(self, polly_client: Any, *, voice: str, sample_rate: int) -> None:
        self._client, self._voice, self._rate = polly_client, voice, sample_rate
        self._cache: dict[str, bytes] = {}
        self._lock = threading.Lock()

    def get(self, text: str) -> bytes:
        with self._lock:
            if text not in self._cache:
                self._cache[text] = synthesize_prompt(
                    text, voice=self._voice, polly_client=self._client, sample_rate=self._rate,
                )
            return self._cache[text]
