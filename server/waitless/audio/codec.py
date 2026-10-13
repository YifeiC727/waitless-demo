"""G.711 μ-law 编解码与 PCM16 工具（tech §6.3、§7；AC-79）。

第 3 步写完、共享冻结（歧义表 A-21，定稿 skeleton_writes_codec）：D 线（线路与转接）和 B 线（Goertzel 测试、
CarrierCall）都只调用，任何线都不改。不依赖 Pipecat，也不依赖 Python 3.13 已移除的 audioop；text-sim 也可导入。

编码采用标准 G.711 μ-law 的 14 位算法（与 Sun g711.c、CPython audioop.lin2ulaw 同一张码表）：
先右移 2 位成 14 位，取绝对值并限幅到 8159，加偏置 33，按段（0–7）取 4 位尾数，最后按符号取反。
解码与 audioop.ulaw2lin 相同：0xFF 与 0x7F 都解码为 0，码值的最大幅度是 ±32124。
PCM16 一律是小端、单声道（§6.3、§6.4）。
"""

import array
import math
import sys

__all__ = ["pcm16_rms_dbfs", "silence_pcm16", "ulaw_decode", "ulaw_encode"]

_FULL_SCALE = 32768.0
_ULAW_BIAS_14 = 33  # 0x84 >> 2
_ULAW_CLIP_14 = 8159
_SEG_END_14 = (0x3F, 0x7F, 0xFF, 0x1FF, 0x3FF, 0x7FF, 0xFFF, 0x1FFF)


def _encode_14(value: int) -> int:
    """一个 14 位样本（−8192…8191）→ μ-law 码。"""
    if value < 0:
        magnitude, mask = -value, 0x7F
    else:
        magnitude, mask = value, 0xFF
    magnitude = min(magnitude, _ULAW_CLIP_14) + _ULAW_BIAS_14
    for seg, end in enumerate(_SEG_END_14):
        if magnitude <= end:
            return ((seg << 4) | ((magnitude >> (seg + 1)) & 0x0F)) ^ mask
    return 0x7F ^ mask  # 限幅后不会走到这里，留作保险


def _decode_code(code: int) -> int:
    """μ-law 码 → 16 位线性样本。"""
    inverted = ~code & 0xFF
    magnitude = (((inverted & 0x0F) << 3) + 0x84) << ((inverted & 0x70) >> 4)
    return (0x84 - magnitude) if inverted & 0x80 else (magnitude - 0x84)


# 14 位样本 + 8192 → 码；码 → 2 字节小端 PCM16。导入时各算一次（16384 + 256 项）。
_ENCODE_TABLE = bytes(_encode_14(v) for v in range(-8192, 8192))
_DECODE_TABLE = tuple(_decode_code(c).to_bytes(2, "little", signed=True) for c in range(256))


def _samples(pcm16: bytes) -> array.array:
    if len(pcm16) % 2:
        raise ValueError("PCM16 data must have an even number of bytes")
    samples = array.array("h")
    samples.frombytes(bytes(pcm16))
    if sys.byteorder == "big":
        samples.byteswap()
    return samples


def ulaw_encode(pcm16: bytes) -> bytes:
    """PCM16 LE 单声道 → μ-law，每个样本 1 字节。字节数为奇数时抛 ValueError。"""
    table = _ENCODE_TABLE
    return bytes(table[(s >> 2) + 8192] for s in _samples(pcm16))


def ulaw_decode(ulaw: bytes) -> bytes:
    """μ-law → PCM16 LE 单声道；0xFF 解码为 0（静音）。"""
    table = _DECODE_TABLE
    return b"".join(table[c] for c in bytes(ulaw))


def pcm16_rms_dbfs(pcm16: bytes) -> float:
    """均方根电平（dBFS，满量程 32768 = 0 dBFS）；空输入或全零返回 -inf。字节数为奇数时抛 ValueError。"""
    samples = _samples(pcm16)
    if not samples:
        return -math.inf
    mean_square = sum(s * s for s in samples) / len(samples)
    if mean_square == 0:
        return -math.inf
    return 20.0 * math.log10(math.sqrt(mean_square) / _FULL_SCALE)


def silence_pcm16(ms: int, sample_rate: int) -> bytes:
    """ms 毫秒的全零 PCM16（按键后的尾静音、转接期间送 Sonic 的零值帧）。
    样本数 = sample_rate × ms // 1000（20 ms @16 kHz = 320 个样本 = 640 字节）。ms < 0 或 sample_rate ≤ 0 抛 ValueError。"""
    if ms < 0:
        raise ValueError("ms must be >= 0")
    if sample_rate <= 0:
        raise ValueError("sample_rate must be > 0")
    return bytes(2 * (sample_rate * ms // 1000))
