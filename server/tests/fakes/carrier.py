"""FakeCarrier：引擎一侧看到的模拟运营商（tech §12、§6.3）。

两部分：
1. HTTP API 替身：`httpx.MockTransport(fake.handle)`（属性 fake.transport），实现
   POST /carrier/calls、POST /carrier/calls/{sid}/hangup、PUT/GET /sim/scenario。
   状态码与错误码同模拟运营商（models.CARRIER_ERROR_CODES 上方的说明；缺口 B-G-3、B-G-4）：拨号 200、
   号码未知 404 unknown_number；挂断未知 call_sid 404 unknown_call；PUT/GET 未知线路 404 unknown_line；
   PUT 的场景不在该线路可用的场景里 404 unknown_scenario，当前选择不变。
   不回显引擎发来的 mark，只记录（缺口 B-G-10，定稿 no_echo）。
   路径按后缀匹配，所以 base_url 可以带前缀；base_url 由测试给出，本文件不写任何主机或端口。
2. 电话线一侧：按 §6.3 生成 Twilio 格式的 connected/start/media/mark/stop 字典，
   并能经 Starlette TestClient 的 WebSocket 会话送出、收回并校验引擎发来的 media/mark/clear。

本替身直接拼字典并用 line_protocol 的模型校验（line_protocol 的编解码函数已由第 3 步写好并冻结，A-23；
这里保持拼字典，替身与被测的编解码各自独立，测试才能发现编解码的错）。
GET /sim/scenario 对未知线路回 404：LiveCall 拨号前查场景失败时 scenario_id 记 null，照常拨号（A-10）。
"""

import itertools
import json
from collections.abc import Iterable, Mapping, Sequence
from typing import Any
from urllib.parse import quote

import httpx
from pydantic import TypeAdapter, ValidationError

from waitless.call.line_protocol import (
    MULAW_FRAME_BYTES,
    SIM_ACCOUNT_SID,
    ConnectedMsg,
    MarkMsg,
    MediaMsg,
    StartMsg,
    StopMsg,
    ToCarrierMessage,
)
from waitless.models import CarrierDialIn, ScenarioSelection

ULAW_SILENCE_BYTE = 0xFF
# §6.10 的三个场景；available 没给时，每条已知线路都可选这三个
DEFAULT_SCENARIO_IDS: tuple[str, ...] = ("baseline", "menu-reworded", "account-holder-required")
_to_carrier = TypeAdapter(ToCarrierMessage)


def ulaw_silence(frames: int = 1) -> bytes:
    return bytes([ULAW_SILENCE_BYTE]) * (MULAW_FRAME_BYTES * frames)


def split_ulaw(data: bytes, frame_bytes: int = MULAW_FRAME_BYTES) -> list[bytes]:
    return [data[i : i + frame_bytes] for i in range(0, len(data), frame_bytes)]


class FakeCarrier:
    def __init__(
        self,
        numbers: Mapping[str, str] | None = None,
        *,
        scenarios: Mapping[str, str] | None = None,
        available: Mapping[str, Iterable[str]] | None = None,
    ) -> None:
        """numbers：可拨打的号码 → 线路号（line_number）；scenarios：线路号 → 当前场景；
        available：线路号 → 该线路所属公司可选的场景（相当于 list_scenarios 的结果）；没给时每条已知线路都用
        DEFAULT_SCENARIO_IDS。已知线路 = numbers 的值 ∪ scenarios 的键 ∪ available 的键。"""
        self.numbers = dict(numbers or {})
        self.scenarios: dict[str, str] = dict(scenarios or {})
        self.available: dict[str, frozenset[str]] | None = (
            {line: frozenset(ids) for line, ids in available.items()} if available is not None else None
        )
        self.dials: list[CarrierDialIn] = []
        self.call_sids: dict[str, CarrierDialIn] = {}
        self.hangups: list[str] = []
        self.requests: list[httpx.Request] = []
        self.received: list[Any] = []
        self.fail_next_dial: tuple[int, dict[str, Any]] | None = None
        self._ids = itertools.count(1)

    # -- HTTP API -----------------------------------------------------------
    @property
    def transport(self) -> httpx.MockTransport:
        return httpx.MockTransport(self.handle)

    def handle(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        path = request.url.path.rstrip("/")
        method = request.method.upper()
        try:
            if method == "POST" and path.endswith("/carrier/calls"):
                return self._dial(request)
            if method == "POST" and path.endswith("/hangup") and "/carrier/calls/" in path:
                sid = path.rsplit("/", 2)[-2]
                return self._hangup(sid)
            if path.endswith("/sim/scenario"):
                if method == "PUT":
                    sel = ScenarioSelection.model_validate(json.loads(request.content or b"{}"))
                    if sel.line_number not in self.known_lines:
                        return _error(404, "unknown_line", f"unknown line {sel.line_number!r}")
                    if sel.scenario_id not in self.scenarios_for(sel.line_number):
                        return _error(404, "unknown_scenario", f"unknown scenario {sel.scenario_id!r}")
                    self.scenarios[sel.line_number] = sel.scenario_id
                    return httpx.Response(200, json=sel.model_dump())
                if method == "GET":
                    line = request.url.params.get("line_number", "")
                    if line not in self.scenarios:
                        return _error(404, "unknown_line", f"no scenario for line {line!r}")
                    return httpx.Response(200, json={"line_number": line, "scenario_id": self.scenarios[line]})
        except (ValidationError, json.JSONDecodeError) as exc:
            return _error(422, "invalid_request", str(exc))
        return _error(404, "not_found", f"{method} {path}")

    @property
    def known_lines(self) -> set[str]:
        return set(self.numbers.values()) | set(self.scenarios) | set(self.available or {})

    def scenarios_for(self, line_number: str) -> frozenset[str]:
        if self.available is not None:
            return self.available.get(line_number, frozenset())
        return frozenset(DEFAULT_SCENARIO_IDS)

    def _dial(self, request: httpx.Request) -> httpx.Response:
        dial = CarrierDialIn.model_validate(json.loads(request.content or b"{}"))
        self.dials.append(dial)
        if self.fail_next_dial is not None:
            status, body = self.fail_next_dial
            self.fail_next_dial = None
            return httpx.Response(status, json=body)
        if dial.to_number not in self.numbers:
            return _error(404, "unknown_number", f"unknown number {dial.to_number!r}")
        sid = self.new_call_sid()
        self.call_sids[sid] = dial
        return httpx.Response(200, json={"call_sid": sid, "status": "queued"})

    def _hangup(self, sid: str) -> httpx.Response:
        if sid not in self.call_sids:
            return _error(404, "unknown_call", f"unknown call {sid!r}")
        self.hangups.append(sid)
        return httpx.Response(200, json={"call_sid": sid, "status": "completed"})

    # -- 电话线一侧 ---------------------------------------------------------
    def new_call_sid(self) -> str:
        return "CA" + f"{next(self._ids):032x}"

    def new_stream_sid(self) -> str:
        return "MZ" + f"{next(self._ids):032x}"

    @staticmethod
    def connect_url(dial: CarrierDialIn) -> str:
        """运营商回连引擎的地址：stream_url 加 ?token=<每通令牌>（§6.3）。"""
        sep = "&" if "?" in dial.stream_url else "?"
        return f"{dial.stream_url}{sep}token={quote(dial.stream_token)}"

    def line_messages(
        self,
        dial: CarrierDialIn,
        call_sid: str,
        ulaw_frames: Iterable[bytes] = (),
        *,
        stream_sid: str | None = None,
        marks: Sequence[str] = (),
        include_stop: bool = True,
    ) -> list[dict[str, Any]]:
        """一通电话从接通到挂断的运营商消息（字典），每条都先过 line_protocol 模型校验。"""
        import base64

        sid = stream_sid or self.new_stream_sid()
        seq = itertools.count(1)
        out: list[dict[str, Any]] = [
            {"event": "connected", "protocol": "Call", "version": "1.0.0"},
            {
                "event": "start",
                "sequenceNumber": str(next(seq)),
                "streamSid": sid,
                "start": {
                    "accountSid": SIM_ACCOUNT_SID,
                    "callSid": call_sid,
                    "streamSid": sid,
                    "tracks": ["inbound"],
                    "customParameters": {"call_id": dial.call_id, "task_id": dial.task_id},
                    "mediaFormat": {"encoding": "audio/x-mulaw", "sampleRate": 8000, "channels": 1},
                },
            },
        ]
        for chunk_no, frame in enumerate(ulaw_frames, start=1):
            out.append(
                {
                    "event": "media",
                    "sequenceNumber": str(next(seq)),
                    "streamSid": sid,
                    "media": {
                        "track": "inbound",
                        "chunk": str(chunk_no),
                        "timestamp": str((chunk_no - 1) * 20),
                        "payload": base64.b64encode(frame).decode("ascii"),
                    },
                }
            )
        for name in marks:
            out.append({"event": "mark", "sequenceNumber": str(next(seq)), "streamSid": sid, "mark": {"name": name}})
        if include_stop:
            out.append(
                {
                    "event": "stop",
                    "sequenceNumber": str(next(seq)),
                    "streamSid": sid,
                    "stop": {"accountSid": SIM_ACCOUNT_SID, "callSid": call_sid},
                }
            )
        for msg in out:
            {"connected": ConnectedMsg, "start": StartMsg, "media": MediaMsg, "mark": MarkMsg, "stop": StopMsg}[
                msg["event"]
            ].model_validate(msg)
        return out

    def play_sync(self, ws: Any, messages: Iterable[Mapping[str, Any]]) -> None:
        """经 Starlette TestClient 的 WebSocketTestSession 逐条送出。"""
        for msg in messages:
            ws.send_text(json.dumps(msg))

    def receive_sync(self, ws: Any, count: int) -> list[Any]:
        """收 count 条引擎消息，逐条按 to_carrier 方向校验（media/mark/clear），并存进 received。"""
        got = []
        for _ in range(count):
            msg = _to_carrier.validate_python(json.loads(ws.receive_text()))
            got.append(msg)
            self.received.append(msg)
        return got


def _error(status: int, code: str, message: str) -> httpx.Response:
    return httpx.Response(status, json={"code": code, "message": message})


__all__ = ["DEFAULT_SCENARIO_IDS", "FakeCarrier", "ULAW_SILENCE_BYTE", "split_ulaw", "ulaw_silence"]
