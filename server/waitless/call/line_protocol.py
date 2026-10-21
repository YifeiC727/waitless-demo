"""电话线协议：Twilio Media Streams 的 JSON 文本帧（tech.v4.md §6.3；AC-61）。

第 3 步写完整份文件（消息模型、签名与全部函数体），共享冻结（歧义表 A-23，定稿 step3_writes_bodies）：
引擎（D 线的 line_endpoint、LineSerializer）和模拟运营商（B 线的 CarrierCall）都只调用，任何线都不改。
协议有问题时在报告里提出，由 Claude 改。

方向：
- to_engine：模拟运营商 → 引擎（/ws/line）：connected、start、media、mark、stop
- to_carrier：引擎 → 模拟运营商：media、mark、clear（由 Pipecat TwilioFrameSerializer 生成）

to_engine 的 media 必须带 sequenceNumber 与 media.track/chunk/timestamp；mark 必须带
sequenceNumber。to_carrier 的 media 只有 payload，mark 与 clear 没有 sequenceNumber。
这些按方向的必填规则在 parse_line_message() 里执行（模型本身两向通用）。

为什么 parse_line_message 带 direction（tech §7 写的是 parse_line_message(text)；第 3 步复核后保留，必填关键字）：
- §6.3 的两个方向消息形状不同：同一个 event 名（media、mark）在运营商→引擎方向必须带 sequenceNumber
  （media 还要 track、chunk、timestamp），在引擎→运营商方向没有这些字段；connected、start、stop 只出现在
  运营商→引擎，clear 只出现在引擎→运营商。只看一条帧本身，分不清「缺了 sequenceNumber 的坏入站帧」和
  「合法的出站帧」，校验只能放宽到两向的并集，AC-61 要拦的缺字段就漏掉了。
- 两端都要解析，方向相反：D 的 line_endpoint 读运营商发来的 connected、start（to_engine）；B 的 CarrierCall
  收引擎经 Pipecat TwilioFrameSerializer 发出的帧（to_carrier）。S0 V4 的实测握手就是 connected→start，
  inv-1 §1.5 记录 serializer 出站 media 只有 payload、打断时生成 clear（twilio.py:186-207）。
- 不设默认值：默认成任一方向都会让另一端在漏写参数时按错的规则放行。

与 tech §7.1 AC-61 机制文字的差异（已记入 skeleton-notes，待设计下一版改文字）：解析失败抛 LineProtocolError
（ValueError 的子类），不直接漏出 pydantic.ValidationError——非 JSON、方向不允许、入站缺字段这几类本来就不是
ValidationError，统一一种异常，D 的端点只需捕获一种。
"""

import base64
import binascii
import json
import uuid
from collections.abc import Mapping
from typing import Annotated, Any, Literal, TypeVar, Union

from pydantic import BaseModel, ConfigDict, Field, TypeAdapter, ValidationError, field_validator, model_validator

LineDirection = Literal["to_engine", "to_carrier"]

STREAM_SID_PATTERN = r"^MZ[0-9a-fA-F]{32}$"
CALL_SID_PATTERN = r"^CA[0-9a-fA-F]{32}$"
DIGITS_PATTERN = r"^[0-9]+$"

# 协议常量：线路固定 8 kHz μ-law 单声道，每帧 20 ms = 160 字节（§6.3）
MULAW_FRAME_BYTES = 160
LINE_FRAME_MS = 20
SIM_ACCOUNT_SID = "SIM"

StreamSid = Annotated[str, Field(pattern=STREAM_SID_PATTERN)]
LineCallSid = Annotated[str, Field(pattern=CALL_SID_PATTERN)]
SeqStr = Annotated[str, Field(pattern=DIGITS_PATTERN)]


class LineProtocolError(ValueError):
    """电话线消息不合法：不是 JSON、事件不认识、该方向不允许、字段缺失或格式错。"""


class _LineModel(BaseModel):
    model_config = ConfigDict(extra="forbid")


_M = TypeVar("_M", bound=_LineModel)


class ConnectedMsg(_LineModel):
    event: Literal["connected"]
    protocol: Literal["Call"]
    version: Literal["1.0.0"]


class MediaFormat(_LineModel):
    encoding: Literal["audio/x-mulaw"]
    sampleRate: Literal[8000]
    channels: Literal[1]


class StartCustomParameters(_LineModel):
    call_id: Annotated[str, Field(min_length=1)]
    task_id: Annotated[str, Field(min_length=1)]


class StartBody(_LineModel):
    accountSid: Annotated[str, Field(min_length=1)]
    callSid: LineCallSid
    streamSid: StreamSid
    tracks: list[Literal["inbound"]] = Field(min_length=1)
    customParameters: StartCustomParameters
    mediaFormat: MediaFormat


class StartMsg(_LineModel):
    event: Literal["start"]
    sequenceNumber: SeqStr
    streamSid: StreamSid
    start: StartBody

    @model_validator(mode="after")
    def _stream_sid_consistent(self) -> "StartMsg":
        if self.start.streamSid != self.streamSid:
            raise ValueError("start.streamSid must equal streamSid")
        return self


def _check_base64(value: str) -> str:
    try:
        base64.b64decode(value, validate=True)
    except (binascii.Error, ValueError) as exc:
        raise ValueError("payload is not valid base64") from exc
    return value


class MediaBody(_LineModel):
    payload: str
    track: Literal["inbound"] | None = None
    chunk: SeqStr | None = None
    timestamp: SeqStr | None = None

    @field_validator("payload")
    @classmethod
    def _payload_base64(cls, value: str) -> str:
        return _check_base64(value)


class MediaMsg(_LineModel):
    event: Literal["media"]
    sequenceNumber: SeqStr | None = None
    streamSid: StreamSid
    media: MediaBody


class MarkBody(_LineModel):
    name: Annotated[str, Field(min_length=1)]


class MarkMsg(_LineModel):
    event: Literal["mark"]
    sequenceNumber: SeqStr | None = None
    streamSid: StreamSid
    mark: MarkBody


class StopBody(_LineModel):
    accountSid: Annotated[str, Field(min_length=1)]
    callSid: LineCallSid


class StopMsg(_LineModel):
    event: Literal["stop"]
    sequenceNumber: SeqStr
    streamSid: StreamSid
    stop: StopBody


class ClearMsg(_LineModel):
    event: Literal["clear"]
    streamSid: StreamSid


LineMessage = Union[ConnectedMsg, StartMsg, MediaMsg, MarkMsg, StopMsg, ClearMsg]
ToEngineMessage = Annotated[
    Union[ConnectedMsg, StartMsg, MediaMsg, MarkMsg, StopMsg], Field(discriminator="event")
]
ToCarrierMessage = Annotated[Union[MediaMsg, MarkMsg, ClearMsg], Field(discriminator="event")]

EVENTS_BY_DIRECTION: dict[str, frozenset[str]] = {
    "to_engine": frozenset({"connected", "start", "media", "mark", "stop"}),
    "to_carrier": frozenset({"media", "mark", "clear"}),
}


# ---------------------------------------------------------------------------
# 编解码（第 3 步写完函数体，共享冻结：歧义表 A-23，定稿 step3_writes_bodies）
# ---------------------------------------------------------------------------

_TO_ENGINE_ADAPTER: TypeAdapter[Any] = TypeAdapter(ToEngineMessage)
_TO_CARRIER_ADAPTER: TypeAdapter[Any] = TypeAdapter(ToCarrierMessage)


def _describe(exc: ValidationError) -> str:
    """把 pydantic 的错误压成一行；不回显输入值（payload 可能很长）。"""
    parts = []
    for err in exc.errors(include_url=False, include_input=False):
        loc = ".".join(str(p) for p in err.get("loc", ()))
        parts.append(f"{loc}: {err.get('msg', 'invalid')}" if loc else str(err.get("msg", "invalid")))
    return "; ".join(parts) or "invalid message"


def _build(model: type[_M], data: dict[str, Any]) -> _M:
    """构造函数的公共出口：参数不合法时抛 LineProtocolError，与解析一致。"""
    try:
        return model.model_validate(data)
    except ValidationError as exc:
        raise LineProtocolError(f"cannot build {model.__name__}: {_describe(exc)}") from exc


def parse_line_message(raw: str | bytes | Mapping[str, Any], *, direction: LineDirection) -> LineMessage:
    """把一条 JSON 文本帧解析成消息模型（AC-61）。

    规则：
    - raw 不是合法 JSON 对象 → LineProtocolError
    - event 不在 EVENTS_BY_DIRECTION[direction] 里 → LineProtocolError
    - 模型校验失败（字段缺失、多余字段、sid 格式、mediaFormat 不是 8 kHz μ-law、payload 不是 base64）
      → LineProtocolError（不把 pydantic.ValidationError 直接漏出去）
    - to_engine 的 media 缺 sequenceNumber 或 media.track/chunk/timestamp → LineProtocolError
    - to_engine 的 mark 缺 sequenceNumber → LineProtocolError

    direction 必须显式给出，理由见模块文档「为什么 parse_line_message 带 direction」。
    """
    if direction not in EVENTS_BY_DIRECTION:
        raise LineProtocolError(f"unknown direction {direction!r}")
    if isinstance(raw, Mapping):
        data: Any = dict(raw)
    else:
        try:
            data = json.loads(raw)
        except (ValueError, TypeError) as exc:  # JSONDecodeError、UnicodeDecodeError 都是 ValueError
            raise LineProtocolError("line message is not valid JSON") from exc
    if not isinstance(data, dict):
        raise LineProtocolError("line message must be a JSON object")
    event = data.get("event")
    if not isinstance(event, str):
        raise LineProtocolError("line message has no event field")
    if event not in EVENTS_BY_DIRECTION[direction]:
        raise LineProtocolError(f"event {event!r} is not allowed {direction.replace('_', ' ')}")
    adapter = _TO_ENGINE_ADAPTER if direction == "to_engine" else _TO_CARRIER_ADAPTER
    try:
        msg = adapter.validate_python(data)
    except ValidationError as exc:
        raise LineProtocolError(f"invalid {event} message: {_describe(exc)}") from exc
    if direction == "to_engine":
        if isinstance(msg, MediaMsg):
            missing = [
                name
                for name, value in (
                    ("sequenceNumber", msg.sequenceNumber),
                    ("media.track", msg.media.track),
                    ("media.chunk", msg.media.chunk),
                    ("media.timestamp", msg.media.timestamp),
                )
                if value is None
            ]
            if missing:
                raise LineProtocolError("inbound media needs " + ", ".join(missing))
        elif isinstance(msg, MarkMsg) and msg.sequenceNumber is None:
            raise LineProtocolError("inbound mark needs sequenceNumber")
    return msg


def encode_line_message(msg: LineMessage) -> str:
    """编码成紧凑 JSON 文本帧；值为 None 的可选字段不出现（§6.3 的消息形状）。"""
    if not isinstance(msg, (ConnectedMsg, StartMsg, MediaMsg, MarkMsg, StopMsg, ClearMsg)):
        raise LineProtocolError(f"not a line message: {type(msg).__name__}")
    return json.dumps(msg.model_dump(mode="json", exclude_none=True), separators=(",", ":"))


def media_payload_bytes(msg: MediaMsg) -> bytes:
    """取出 media.payload 的 μ-law 字节（base64 解码）。"""
    return base64.b64decode(msg.media.payload, validate=True)


def _b64(ulaw: bytes) -> str:
    return base64.b64encode(bytes(ulaw)).decode("ascii")


def make_connected() -> ConnectedMsg:
    """{"event":"connected","protocol":"Call","version":"1.0.0"}"""
    return _build(ConnectedMsg, {"event": "connected", "protocol": "Call", "version": "1.0.0"})


def make_start(
    *,
    sequence: int,
    stream_sid: str,
    call_sid: str,
    call_id: str,
    task_id: str,
    account_sid: str = SIM_ACCOUNT_SID,
) -> StartMsg:
    """start 消息：tracks=["inbound"]，mediaFormat 固定 audio/x-mulaw 8000 单声道。"""
    return _build(
        StartMsg,
        {
            "event": "start",
            "sequenceNumber": str(sequence),
            "streamSid": stream_sid,
            "start": {
                "accountSid": account_sid,
                "callSid": call_sid,
                "streamSid": stream_sid,
                "tracks": ["inbound"],
                "customParameters": {"call_id": call_id, "task_id": task_id},
                "mediaFormat": {"encoding": "audio/x-mulaw", "sampleRate": 8000, "channels": 1},
            },
        },
    )


def make_media_in(*, sequence: int, stream_sid: str, chunk: int, timestamp_ms: int, ulaw: bytes) -> MediaMsg:
    """运营商发给引擎的 media：带 sequenceNumber、track=inbound、chunk、timestamp（毫秒）。
    ulaw 一般是 MULAW_FRAME_BYTES 字节；这里不强制长度（最后一帧可以更短）。"""
    return _build(
        MediaMsg,
        {
            "event": "media",
            "sequenceNumber": str(sequence),
            "streamSid": stream_sid,
            "media": {"track": "inbound", "chunk": str(chunk), "timestamp": str(timestamp_ms), "payload": _b64(ulaw)},
        },
    )


def make_media_out(*, stream_sid: str, ulaw: bytes) -> MediaMsg:
    """引擎发给运营商的 media：只有 payload。"""
    return _build(MediaMsg, {"event": "media", "streamSid": stream_sid, "media": {"payload": _b64(ulaw)}})


def make_mark(*, stream_sid: str, name: str, sequence: int | None = None) -> MarkMsg:
    """mark：运营商发出时带 sequence，引擎发出时不带。"""
    data: dict[str, Any] = {"event": "mark", "streamSid": stream_sid, "mark": {"name": name}}
    if sequence is not None:
        data["sequenceNumber"] = str(sequence)
    return _build(MarkMsg, data)


def make_clear(*, stream_sid: str) -> ClearMsg:
    return _build(ClearMsg, {"event": "clear", "streamSid": stream_sid})


def make_stop(*, sequence: int, stream_sid: str, call_sid: str, account_sid: str = SIM_ACCOUNT_SID) -> StopMsg:
    return _build(
        StopMsg,
        {
            "event": "stop",
            "sequenceNumber": str(sequence),
            "streamSid": stream_sid,
            "stop": {"accountSid": account_sid, "callSid": call_sid},
        },
    )


def new_stream_sid() -> str:
    """"MZ" + 32 位小写十六进制，每次不同。"""
    return "MZ" + uuid.uuid4().hex


def new_call_sid() -> str:
    """"CA" + 32 位小写十六进制，每次不同。"""
    return "CA" + uuid.uuid4().hex


__all__ = [
    "CALL_SID_PATTERN",
    "EVENTS_BY_DIRECTION",
    "LINE_FRAME_MS",
    "MULAW_FRAME_BYTES",
    "SIM_ACCOUNT_SID",
    "STREAM_SID_PATTERN",
    "ClearMsg",
    "ConnectedMsg",
    "LineDirection",
    "LineMessage",
    "LineProtocolError",
    "MarkBody",
    "MarkMsg",
    "MediaBody",
    "MediaFormat",
    "MediaMsg",
    "StartBody",
    "StartCustomParameters",
    "StartMsg",
    "StopBody",
    "StopMsg",
    "ToCarrierMessage",
    "ToEngineMessage",
    "encode_line_message",
    "make_clear",
    "make_connected",
    "make_mark",
    "make_media_in",
    "make_media_out",
    "make_start",
    "make_stop",
    "media_payload_bytes",
    "new_call_sid",
    "new_stream_sid",
    "parse_line_message",
]
