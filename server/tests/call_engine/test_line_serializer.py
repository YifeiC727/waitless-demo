"""AC-61（D 侧）：线路两头的消息都过 line_protocol 的模型校验。

line_protocol 的模型与编解码由第 3 步写好并冻结（A-23），它本身的测试是 server/tests/test_line_protocol.py。
这里测 D 线的 LineSerializer（TwilioFrameSerializer 子类）：
- 收：stop 变成 EndFrame（父类返回 None，inv-1 §1.5，K5）；media 交给父类转成管线采样率的 InputAudioRawFrame；
- 发：引擎送出的音频与打断，序列化成的 JSON 按 to_carrier 方向能被 parse_line_message 接受（§6.3）。
"""

import base64
import json
from typing import Any

from pipecat.clocks.system_clock import SystemClock
from pipecat.frames.frames import EndFrame, InputAudioRawFrame, InterruptionFrame, OutputAudioRawFrame
from pipecat.processors.frame_processor import FrameProcessorSetup
from pipecat.serializers.twilio import TwilioFrameSerializer
from pipecat.utils.asyncio.task_manager import TaskManager

from call_engine._support import STEP_MS, tone_pcm16
from fakes import ulaw_silence
from waitless.call import line_protocol as lp

SID = "MZ" + "1" * 32
CALL = "CA" + "2" * 32


async def make_serializer(engine: Any) -> Any:
    """按 line_ws.LineSerializer 文档的参数构造（auto_hang_up=False，管线输入采样率取 engine.yaml）。"""
    import asyncio

    from waitless.call.line_ws import LineSerializer

    ser = LineSerializer(
        stream_sid=SID,
        params=TwilioFrameSerializer.InputParams(
            sample_rate=engine.audio.pipeline_in_sample_rate, auto_hang_up=False
        ),
    )
    clock = SystemClock()
    clock.start()
    await ser.setup(
        FrameProcessorSetup(
            clock=clock,
            task_manager=TaskManager(loop=asyncio.get_running_loop()),
            pipeline_worker=None,
            audio_in_sample_rate=engine.audio.pipeline_in_sample_rate,
        )
    )
    return ser


def stop_msg() -> str:
    return json.dumps({"event": "stop", "sequenceNumber": "9", "streamSid": SID, "stop": {"accountSid": "SIM", "callSid": CALL}})


def media_msg(seq: int, payload: bytes) -> str:
    return json.dumps(
        {
            "event": "media",
            "sequenceNumber": str(seq),
            "streamSid": SID,
            "media": {
                "track": "inbound",
                "chunk": str(seq),
                "timestamp": str((seq - 1) * STEP_MS),
                "payload": base64.b64encode(payload).decode("ascii"),
            },
        }
    )


async def test_ac61_stop_message_becomes_end_frame(engine: Any) -> None:
    ser = await make_serializer(engine)
    frame = await ser.deserialize(stop_msg())
    assert isinstance(frame, EndFrame)


async def test_ac61_inbound_media_reaches_pipeline_rate(engine: Any) -> None:
    ser = await make_serializer(engine)
    frames = [await ser.deserialize(media_msg(i, ulaw_silence())) for i in range(1, 9)]
    audio = [f for f in frames if f is not None]
    assert audio, "eight 20 ms media frames produced no audio frame"
    assert all(isinstance(f, InputAudioRawFrame) for f in audio)
    assert {f.sample_rate for f in audio} == {engine.audio.pipeline_in_sample_rate}
    assert {f.num_channels for f in audio} == {1}


async def test_ac61_connected_start_and_mark_produce_no_frame(engine: Any) -> None:
    ser = await make_serializer(engine)
    msgs = [
        {"event": "connected", "protocol": "Call", "version": "1.0.0"},
        {
            "event": "start",
            "sequenceNumber": "1",
            "streamSid": SID,
            "start": {
                "accountSid": "SIM",
                "callSid": CALL,
                "streamSid": SID,
                "tracks": ["inbound"],
                "customParameters": {"call_id": "call-d-1", "task_id": "task-d-1"},
                "mediaFormat": {"encoding": "audio/x-mulaw", "sampleRate": 8000, "channels": 1},
            },
        },
        {"event": "mark", "sequenceNumber": "3", "streamSid": SID, "mark": {"name": "menu-end"}},
    ]
    for msg in msgs:
        assert await ser.deserialize(json.dumps(msg)) is None, msg["event"]


async def test_ac61_engine_output_parses_as_to_carrier(engine: Any) -> None:
    ser = await make_serializer(engine)
    rate = engine.audio.pipeline_in_sample_rate
    texts = []
    for _ in range(15):  # 300 ms：Pipecat 的流式重采样器约每 100 ms 吐一次
        out = await ser.serialize(OutputAudioRawFrame(audio=tone_pcm16(STEP_MS, rate), sample_rate=rate, num_channels=1))
        if out is not None:
            texts.append(out)
    assert texts, "no media message for 300 ms of engine audio"
    for text in texts:
        msg = lp.parse_line_message(text, direction="to_carrier")
        assert isinstance(msg, lp.MediaMsg) and msg.streamSid == SID
        assert msg.sequenceNumber is None and msg.media.track is None  # to_carrier 的 media 只有 payload
        assert lp.media_payload_bytes(msg)
    clear = lp.parse_line_message(await ser.serialize(InterruptionFrame()), direction="to_carrier")
    assert isinstance(clear, lp.ClearMsg) and clear.streamSid == SID
