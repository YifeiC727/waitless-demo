"""B 线 · 音频部分（10/15）：语音菜单（Polly）与等待音（tech §3、§6.10；I-2 的 AC-95 的 B 侧前提）。

Polly 一律用 tests/fakes 的 FakePolly（形状同 boto3 polly 客户端，默认测试不出网、不读密钥）。
断言看 FakePolly 收到的请求和函数返回的 PCM 字节。
"""

import pytest

from waitless.company.ivr_audio import PromptCache, hold_music, synthesize_prompt

from ._signals import WIRE_RATE, rms_dbfs

PROMPT = "Thank you for calling Lakeshore Energy. For new service, press 1."


def expected_pcm(call: dict) -> bytes:
    from fakes import FakePolly

    return FakePolly().synthesize_speech(**call)["AudioStream"].read()


@pytest.mark.parametrize("rate", [WIRE_RATE, 16000])
def test_ac95_ivr_prompt_uses_polly_neural_pcm(fake_polly, rate: int) -> None:  # type: ignore[no-untyped-def]
    pcm = synthesize_prompt(PROMPT, voice="Joanna", polly_client=fake_polly, sample_rate=rate)
    assert len(fake_polly.calls) == 1
    call = fake_polly.calls[0]
    assert call["Engine"] == "neural"
    assert call["VoiceId"] == "Joanna"
    assert call["OutputFormat"] == "pcm"
    assert call["SampleRate"] == str(rate), "Polly takes SampleRate as a string"
    assert call["Text"] == PROMPT
    assert pcm == expected_pcm(call), "return the whole PCM16 stream Polly produced, unchanged"


def test_ac95_ivr_prompt_polly_error_is_not_swallowed() -> None:
    from fakes import FakePolly

    polly = FakePolly(fail_with=RuntimeError("polly throttled"))
    with pytest.raises(Exception) as err:
        synthesize_prompt(PROMPT, voice="Joanna", polly_client=polly, sample_rate=WIRE_RATE)
    assert not isinstance(err.value, NotImplementedError), "synthesize_prompt is not implemented"
    chain = [err.value, err.value.__cause__, err.value.__context__]
    assert any(e is not None and "polly throttled" in str(e) for e in chain), "no silent fallback to silence (C14)"


def test_ac95_prompt_cache_synthesizes_each_text_once(fake_polly) -> None:  # type: ignore[no-untyped-def]
    cache = PromptCache(fake_polly, voice="Joanna", sample_rate=WIRE_RATE)
    a1 = cache.get("Press 1 for new service.")
    a2 = cache.get("Press 1 for new service.")
    b = cache.get("Please hold while we connect you to an agent.")
    assert a1 == a2 and a1 != b
    assert [c["Text"] for c in fake_polly.calls] == [
        "Press 1 for new service.",
        "Please hold while we connect you to an agent.",
    ]
    assert all(c["VoiceId"] == "Joanna" and c["SampleRate"] == str(WIRE_RATE) for c in fake_polly.calls)


@pytest.mark.parametrize("rate", [WIRE_RATE, 16000])
def test_ac95_hold_music_is_synthesized_and_audible(rate: int) -> None:
    two = hold_music(2.0, sample_rate=rate)
    assert len(two) % 2 == 0
    assert abs(len(two) // 2 - 2 * rate) <= 1, "hold_music(seconds) returns that many seconds of PCM16"
    one = hold_music(1.0, sample_rate=rate)
    assert abs(len(one) // 2 - rate) <= 1
    level = rms_dbfs(two)
    assert -40.0 < level < -3.0, f"hold music should be audible but not clipping, got {level:.1f} dBFS"
    assert hold_music(2.0, sample_rate=rate) == two, "programmatic hold music is deterministic"
