"""语音菜单与等待音（B 线；tech §3 Polly、§6.10 hold）。

公开接口（§7）：synthesize_prompt()、hold_music()。Polly 只在 live-sim 用；测试用 tests/fakes 的 FakePolly。
不把合成的音频写进仓库（AC-100）；如需缓存，只放内存或运行目录。
"""

from typing import Any

__all__ = ["PromptCache", "hold_music", "synthesize_prompt"]


def synthesize_prompt(text: str, *, voice: str, polly_client: Any, sample_rate: int) -> bytes:
    """polly_client.synthesize_speech(Engine="neural", VoiceId=voice, OutputFormat="pcm",
    SampleRate=str(sample_rate), Text=text) → PCM16 LE 单声道。boto3 服务名只能是 polly（AC-31）。"""
    raise NotImplementedError


def hold_music(seconds: float, *, sample_rate: int) -> bytes:
    """程序合成的等待音（不用外部音频文件）。"""
    raise NotImplementedError


class PromptCache:
    """同一文字只合成一次（内存缓存）。"""

    def __init__(self, polly_client: Any, *, voice: str, sample_rate: int) -> None:
        raise NotImplementedError

    def get(self, text: str) -> bytes:
        raise NotImplementedError
