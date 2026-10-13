"""B 线测试用的音频信号工具（第 3 步写，冻结）。只在测试里用，不是产品代码。

- μ-law 一律经冻结的 waitless/audio/codec.py（歧义表 A-21 定稿）。
- Pipecat 自带的按键音（AC-79 原文）：pipecat_dtmf_pcm() 按 OutputDTMFUrgentFrame 的 buttons 逐个取
  load_dtmf_audio()——与输出端 _write_dtmf_audio() 发出的是同一段音频（inv-1 §1.6，base_output.py:305-318）。
- 合成 DTMF 双音（隐藏组的变体用）：按 S0 V4 实测的 Pipecat 内置音调复刻——每个按键 300 ms 音、200 ms 静音，
  两个频率等幅（各 2048），合成 RMS 约 −24.08 dBFS、峰值约 −18 dBFS。
- 合成人声（AC-80）：多谐波浊音（基频抖动、颤音、共振峰包络、音节包络）与带包络的噪声；
  不用任何录音文件（AC-100）。

线路采样率不写死：由 line_protocol 的协议常量推出（160 字节 μ-law = 20 ms → 8000 Hz）。
"""

from collections.abc import Iterable, Sequence

import numpy as np

from waitless.call.line_protocol import LINE_FRAME_MS, MULAW_FRAME_BYTES

WIRE_RATE = MULAW_FRAME_BYTES * 1000 // LINE_FRAME_MS  # 8000：线路 μ-law 每样本 1 字节
FULL_SCALE = 32768.0

DTMF_ROW_HZ = (697.0, 770.0, 852.0, 941.0)
DTMF_COL_HZ = (1209.0, 1336.0, 1477.0)
DTMF_LAYOUT = (("1", "2", "3"), ("4", "5", "6"), ("7", "8", "9"), ("*", "0", "#"))
ALL_KEYS = tuple(k for row in DTMF_LAYOUT for k in row)

# S0 V4 实测（lanes/s0/report-s0-r1.md §V4）：Pipecat 内置音调
PIPECAT_TONE_MS = 300
PIPECAT_GAP_MS = 200
PIPECAT_LEVEL_DBFS = -24.08


# ---------------------------------------------------------------------------
# μ-law：用冻结的 audio/codec.py（歧义表 A-21 定稿：第 3 步写好、B 直接调用）
# ---------------------------------------------------------------------------


def ulaw_roundtrip(pcm16: bytes) -> bytes:
    """线路上走一遍：PCM16 → μ-law → PCM16（AC-79「经 μ-law 编解码后」）。"""
    from waitless.audio.codec import ulaw_decode, ulaw_encode

    return ulaw_decode(ulaw_encode(pcm16))


def ulaw_encode(pcm16: bytes) -> bytes:
    from waitless.audio.codec import ulaw_encode as enc

    return enc(pcm16)


def ulaw_decode(ulaw: bytes) -> bytes:
    from waitless.audio.codec import ulaw_decode as dec

    return dec(ulaw)


# ---------------------------------------------------------------------------
# 基础
# ---------------------------------------------------------------------------


def n_samples(ms: float, rate: int) -> int:
    return int(round(rate * ms / 1000.0))


def silence(ms: float, rate: int = WIRE_RATE) -> bytes:
    return b"\x00\x00" * n_samples(ms, rate)


def to_pcm16(x: np.ndarray) -> bytes:
    return np.clip(np.round(x), -32768, 32767).astype("<i2").tobytes()


def rms_dbfs(pcm16: bytes) -> float:
    s = np.frombuffer(pcm16, dtype="<i2").astype(np.float64)
    if s.size == 0 or not np.any(s):
        return float("-inf")
    return 20.0 * np.log10(np.sqrt(np.mean(s * s)) / FULL_SCALE)


def _scale_to_dbfs(x: np.ndarray, level_dbfs: float) -> np.ndarray:
    rms = float(np.sqrt(np.mean(x * x))) if x.size else 0.0
    if rms == 0.0:
        return x
    return x * (FULL_SCALE * 10 ** (level_dbfs / 20.0) / rms)


def key_freqs(key: str) -> tuple[float, float]:
    for r, row in enumerate(DTMF_LAYOUT):
        if key in row:
            return DTMF_ROW_HZ[r], DTMF_COL_HZ[row.index(key)]
    raise ValueError(f"not a DTMF key: {key!r}")


# ---------------------------------------------------------------------------
# DTMF
# ---------------------------------------------------------------------------


async def pipecat_dtmf_pcm(keys: str, *, rate: int = WIRE_RATE) -> bytes:
    """Pipecat 1.12.0 输出端为 OutputDTMFUrgentFrame.from_string(keys) 写出的带内音调（PCM16，rate 采样率）。"""
    from pipecat.audio.dtmf.utils import load_dtmf_audio
    from pipecat.frames.frames import OutputDTMFUrgentFrame

    frame = OutputDTMFUrgentFrame.from_string(keys)
    return b"".join([await load_dtmf_audio(button, sample_rate=rate) for button in frame.buttons])


def dtmf_tone(
    key: str,
    ms: float = PIPECAT_TONE_MS,
    *,
    rate: int = WIRE_RATE,
    level_dbfs: float = PIPECAT_LEVEL_DBFS,
    twist_db: float = 0.0,
    freq_scale: float = 1.0,
) -> bytes:
    """一个按键的双音。level_dbfs 是两音合成后的 RMS；twist_db>0 表示高频组比低频组高。"""
    lo, hi = key_freqs(key)
    n = n_samples(ms, rate)
    t = np.arange(n) / rate
    a_lo = 1.0
    a_hi = 10 ** (twist_db / 20.0)
    x = a_lo * np.sin(2 * np.pi * lo * freq_scale * t) + a_hi * np.sin(2 * np.pi * hi * freq_scale * t)
    # 两个正弦的合成 RMS = sqrt((a_lo² + a_hi²) / 2)
    target = FULL_SCALE * 10 ** (level_dbfs / 20.0)
    x *= target / np.sqrt((a_lo**2 + a_hi**2) / 2.0)
    return to_pcm16(x)


def dtmf_sequence(
    keys: str,
    *,
    rate: int = WIRE_RATE,
    tone_ms: float = PIPECAT_TONE_MS,
    gap_ms: float = PIPECAT_GAP_MS,
    lead_ms: float = 0.0,
    tail_ms: float = 200.0,
    level_dbfs: float = PIPECAT_LEVEL_DBFS,
    twist_db: float = 0.0,
    freq_scale: float = 1.0,
) -> bytes:
    """按 Pipecat 的发法：每个按键「音 + 间隔」，最后补尾静音（engine.yaml dtmf.tail_silence_ms 的作用）。"""
    out = [silence(lead_ms, rate)]
    for i, k in enumerate(keys):
        out.append(dtmf_tone(k, tone_ms, rate=rate, level_dbfs=level_dbfs, twist_db=twist_db, freq_scale=freq_scale))
        if i < len(keys) - 1:
            out.append(silence(gap_ms, rate))
    out.append(silence(tail_ms, rate))
    return b"".join(out)


def single_tone(freqs: Sequence[float], ms: float, *, rate: int = WIRE_RATE, level_dbfs: float = -24.0) -> bytes:
    """任意几个等幅正弦（用于「只有一组频率」「同组两个频率」等反例）。"""
    n = n_samples(ms, rate)
    t = np.arange(n) / rate
    x = sum(np.sin(2 * np.pi * f * t) for f in freqs)
    return to_pcm16(_scale_to_dbfs(np.asarray(x, dtype=np.float64), level_dbfs))


def add_noise(pcm16: bytes, *, snr_db: float, seed: int = 0) -> bytes:
    s = np.frombuffer(pcm16, dtype="<i2").astype(np.float64)
    p_sig = float(np.mean(s * s)) or 1.0
    rng = np.random.default_rng(seed)
    noise = rng.standard_normal(s.size) * np.sqrt(p_sig / 10 ** (snr_db / 10.0))
    return to_pcm16(s + noise)


def mix(a: bytes, b: bytes) -> bytes:
    x = np.frombuffer(a, dtype="<i2").astype(np.float64)
    y = np.frombuffer(b, dtype="<i2").astype(np.float64)
    n = max(x.size, y.size)
    out = np.zeros(n)
    out[: x.size] += x
    out[: y.size] += y
    return to_pcm16(out)


# ---------------------------------------------------------------------------
# 合成人声（AC-80）：不是录音
# ---------------------------------------------------------------------------

VOWEL_NEUTRAL = ((500.0, 80.0), (1500.0, 110.0), (2500.0, 160.0))  # 央元音 /ə/
VOWEL_A = ((730.0, 90.0), (1090.0, 110.0), (2440.0, 160.0))  # /ɑ/
VOWEL_I = ((270.0, 60.0), (2290.0, 120.0), (3010.0, 200.0))  # /i/


def _formant_gain(f: np.ndarray, formants: Sequence[tuple[float, float]]) -> np.ndarray:
    g = np.zeros_like(f)
    for fc, bw in formants:
        g += 1.0 / np.sqrt(1.0 + ((f - fc) / (bw / 2.0)) ** 2)
    return g * (100.0 / np.maximum(f, 100.0)) ** 0.5  # 声门谱倾斜


def syllable_envelope(n: int, rate: int, *, syllable_hz: float = 4.0, seed: int = 0, pause_every: int = 5) -> np.ndarray:
    """音节包络：每个音节一段升余弦，每隔几个音节停顿一下。"""
    rng = np.random.default_rng(seed + 101)
    env = np.zeros(n)
    pos = 0
    count = 0
    while pos < n:
        dur = int(rate / syllable_hz * rng.uniform(0.7, 1.3))
        seg = 0.5 * (1 - np.cos(2 * np.pi * np.arange(min(dur, n - pos)) / max(dur, 1)))
        env[pos : pos + seg.size] = seg * rng.uniform(0.6, 1.0)
        pos += dur
        count += 1
        if count % pause_every == 0:
            pos += int(rate * rng.uniform(0.08, 0.2))
    return env


def voiced_speech(
    seconds: float,
    *,
    rate: int = WIRE_RATE,
    f0: float = 120.0,
    formants: Sequence[tuple[float, float]] = VOWEL_NEUTRAL,
    level_dbfs: float = -20.0,
    vibrato_hz: float = 5.0,
    vibrato_depth: float = 0.02,
    glide: float = 0.12,
    jitter: float = 0.01,
    syllable_hz: float = 4.0,
    seed: int = 0,
) -> bytes:
    """多谐波浊音：基频随时间缓慢起伏（语调）、带颤音和抖动，各次谐波按共振峰包络加权，再乘音节包络。"""
    rng = np.random.default_rng(seed)
    n = n_samples(seconds * 1000, rate)
    t = np.arange(n) / rate
    contour = 1.0 + glide * np.sin(2 * np.pi * 0.35 * t + rng.uniform(0, 2 * np.pi))
    vib = 1.0 + vibrato_depth * np.sin(2 * np.pi * vibrato_hz * t + rng.uniform(0, 2 * np.pi))
    jit = 1.0 + jitter * np.convolve(rng.standard_normal(n), np.ones(80) / 80, mode="same")
    inst_f0 = f0 * contour * vib * jit
    phase = 2 * np.pi * np.cumsum(inst_f0) / rate
    nyq = rate * 0.45
    x = np.zeros(n)
    k_max = int(nyq / (f0 * (1 - glide - vibrato_depth))) + 1
    for k in range(1, k_max + 1):
        fk = k * inst_f0
        amp = np.where(fk < nyq, _formant_gain(fk, formants), 0.0)
        x += amp * np.sin(k * phase + rng.uniform(0, 2 * np.pi))
    x *= syllable_envelope(n, rate, syllable_hz=syllable_hz, seed=seed)
    return to_pcm16(_scale_to_dbfs(x, level_dbfs))


def enveloped_noise(
    seconds: float,
    *,
    rate: int = WIRE_RATE,
    level_dbfs: float = -26.0,
    smooth: int = 3,
    syllable_hz: float = 5.0,
    seed: int = 0,
) -> bytes:
    """带音节包络的噪声（清辅音、气声、耳语）。smooth>1 时做简单低通，模拟偏低频的气流声。"""
    rng = np.random.default_rng(seed)
    n = n_samples(seconds * 1000, rate)
    noise = rng.standard_normal(n)
    if smooth > 1:
        noise = np.convolve(noise, np.ones(smooth) / smooth, mode="same")
    x = noise * syllable_envelope(n, rate, syllable_hz=syllable_hz, seed=seed)
    return to_pcm16(_scale_to_dbfs(x, level_dbfs))


def conversation(seconds: float, *, rate: int = WIRE_RATE, seed: int = 0, voices: Iterable[float] = (120.0, 210.0)) -> bytes:
    """几段浊音与清音交替、夹着停顿，模拟线路上 Sonic 与坐席的人声。"""
    rng = np.random.default_rng(seed)
    parts: list[bytes] = []
    total = 0.0
    voices = tuple(voices)
    i = 0
    vowels = (VOWEL_NEUTRAL, VOWEL_A, VOWEL_I)
    while total < seconds:
        dur = float(rng.uniform(0.4, 0.9))
        f0 = voices[i % len(voices)] * float(rng.uniform(0.9, 1.1))
        parts.append(voiced_speech(dur, rate=rate, f0=f0, formants=vowels[i % 3], seed=seed + i))
        parts.append(enveloped_noise(float(rng.uniform(0.08, 0.2)), rate=rate, seed=seed + 50 + i))
        parts.append(silence(float(rng.uniform(40, 160)), rate))
        total += dur + 0.3
        i += 1
    return b"".join(parts)


# ---------------------------------------------------------------------------
# 喂给识别器
# ---------------------------------------------------------------------------


def frame_bytes(rate: int, ms: int = LINE_FRAME_MS) -> int:
    return n_samples(ms, rate) * 2


def chunks(data: bytes, size: int) -> list[bytes]:
    return [data[i : i + size] for i in range(0, len(data), size)]


def feed_all(detector: object, pcm16: bytes, chunk_bytes: int) -> list[str]:
    """按 chunk_bytes 分段喂入，按顺序收集识别出的按键。"""
    got: list[str] = []
    for c in chunks(pcm16, chunk_bytes):
        out = detector.feed(c)  # type: ignore[attr-defined]
        assert isinstance(out, list), f"feed() must return a list, got {type(out).__name__}"
        got.extend(out)
    return got
