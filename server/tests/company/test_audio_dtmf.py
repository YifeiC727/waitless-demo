"""B 线 · 音频部分（10/15）：Goertzel 按键识别（tech §7.1 AC-79、AC-80；S0 V4；K15）。

AC-79 按原文（歧义表 A-21 定稿）：Pipecat 1.12.0 为 OutputDTMFUrgentFrame.from_string("19#") 写出的带内音调，
经冻结的 audio/codec.py 先 ulaw_encode 再 ulaw_decode，按 20 ms 分段喂给 GoertzelDetector.feed()。
AC-80：静音、空闲线路、合成人声（多谐波浊音、带包络的噪声、几种交替的对话）都不许报出按键。
人声全部在测试里用 numpy 合成，不用录音文件（AC-100）。
"""

import pytest

from waitless.company.dtmf_detect import GoertzelDetector

from ._signals import (
    ALL_KEYS,
    WIRE_RATE,
    conversation,
    enveloped_noise,
    feed_all,
    frame_bytes,
    pipecat_dtmf_pcm,
    silence,
    ulaw_decode,
    ulaw_roundtrip,
    voiced_speech,
)

FRAME = frame_bytes(WIRE_RATE)  # 20 ms PCM16 = 320 字节
TAIL = silence(200)  # engine.yaml dtmf.tail_silence_ms


def detector() -> GoertzelDetector:
    return GoertzelDetector(sample_rate=WIRE_RATE)


# ---------------------------------------------------------------------------
# AC-79
# ---------------------------------------------------------------------------


async def test_ac79_dtmf_roundtrip() -> None:
    pcm = ulaw_roundtrip(await pipecat_dtmf_pcm("19#") + TAIL)
    assert feed_all(detector(), pcm, FRAME) == ["1", "9", "#"]


@pytest.mark.parametrize("key", ALL_KEYS)
async def test_ac79_dtmf_roundtrip_every_key(key: str) -> None:
    pcm = ulaw_roundtrip(await pipecat_dtmf_pcm(key) + TAIL)
    assert feed_all(detector(), pcm, FRAME) == [key]


async def test_ac79_dtmf_roundtrip_account_number_entry() -> None:
    # press_field:account_number 会键入「账户号 + #」，同一键连按也要逐个识别
    keys = "48155112#"
    pcm = ulaw_roundtrip(await pipecat_dtmf_pcm(keys) + TAIL)
    assert feed_all(detector(), pcm, FRAME) == list(keys)


async def test_ac79_dtmf_roundtrip_same_result_for_any_chunking() -> None:
    pcm = ulaw_roundtrip(await pipecat_dtmf_pcm("19#") + TAIL)
    assert feed_all(detector(), pcm, len(pcm)) == ["1", "9", "#"]  # 一次喂完
    assert feed_all(detector(), pcm, 2 * 80) == ["1", "9", "#"]  # 10 ms 一段


# ---------------------------------------------------------------------------
# AC-80
# ---------------------------------------------------------------------------

SAMPLES = {
    "digital_silence": lambda: silence(2000),
    "idle_ulaw_line": lambda: ulaw_decode(b"\xff" * (WIRE_RATE * 2)),
    "male_voice": lambda: voiced_speech(3.0, f0=120.0, seed=1),
    "female_voice": lambda: voiced_speech(3.0, f0=210.0, seed=2),
    "breathy_noise": lambda: enveloped_noise(2.0, seed=3),
    "conversation": lambda: conversation(5.0, seed=4),
}


@pytest.mark.parametrize("name", sorted(SAMPLES))
def test_ac80_dtmf_false_positive(name: str) -> None:
    pcm = ulaw_roundtrip(SAMPLES[name]())
    assert feed_all(detector(), pcm, FRAME) == [], f"{name} must not be reported as a key press"
