"""共用测试替身（第 3 步写，冻结；tech §12）。

server/tests 不是包，pytest 会把 conftest 所在目录放进 sys.path，所以测试里这样导入：

    from fakes import FakeJev, FakeClock

FakeSonicService 在没装 Pipecat 的环境里退化为纯记录对象（HAVE_PIPECAT=False）。
FakeTrace 是 D、E、G 共用的内存 trace（缺口 G-G-01），写入检查与 interfaces.Trace.event 一致，不合格抛 ValueError。
"""

from fakes.carrier import FakeCarrier, split_ulaw, ulaw_silence
from fakes.clock import FakeClock
from fakes.converse import FakeConverse, FakeThrottlingError, converse_text, converse_tool_use, forced_tool_name
from fakes.jev import FakeJev, JevCall, jev_choice, jev_noul, jev_score, round_answers
from fakes.polly import FakeAudioStream, FakePolly, pcm16_duration_ms, tone_pcm16
from fakes.sonic import HAVE_PIPECAT, FakeSonicService, Injection, SonicFrameEvent, SonicResponse, default_frame_factory
from fakes.trace import FakeTrace, write_problems

__all__ = [
    "HAVE_PIPECAT",
    "FakeAudioStream",
    "FakeCarrier",
    "FakeClock",
    "FakeConverse",
    "FakeJev",
    "FakePolly",
    "FakeSonicService",
    "FakeThrottlingError",
    "FakeTrace",
    "Injection",
    "JevCall",
    "SonicFrameEvent",
    "SonicResponse",
    "converse_text",
    "converse_tool_use",
    "default_frame_factory",
    "forced_tool_name",
    "jev_choice",
    "jev_noul",
    "jev_score",
    "pcm16_duration_ms",
    "round_answers",
    "split_ulaw",
    "tone_pcm16",
    "ulaw_silence",
    "write_problems",
]
