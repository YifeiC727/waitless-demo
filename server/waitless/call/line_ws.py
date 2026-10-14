"""`/ws/line` 端点、每通线路令牌、stop → EndFrame 的 serializer（D 线；tech §6.3；AC-61、AC-62；K5）。

公开接口（§7）：line_endpoint(websocket)、LineTokens.issue()/verify()、LineSerializer。
"""

from typing import Any
import secrets
from pipecat.frames.frames import EndFrame
from starlette.status import WS_1002_PROTOCOL_ERROR
from starlette.websockets import WebSocketDisconnect
from waitless.call.line_protocol import parse_line_message, ConnectedMsg, StartMsg, StopMsg, LineProtocolError
from waitless.models import WS_CLOSE_UNAUTHORIZED


from pipecat.serializers.twilio import TwilioFrameSerializer

from waitless.interfaces import Clock

__all__ = ["LineSerializer", "LineTokens", "line_endpoint"]


class LineTokens:
    """实现 interfaces.LineTokenIssuer。拨号时签发（LiveCall.start），握手时校验；通话结束即作废。"""

    def __init__(self, clock: Clock) -> None:
        self.clock = clock
        self._tokens = {}

    def issue(self, call_id: str) -> str:
        """secrets.token_urlsafe；只存哈希或在内存里，不写日志、不进 trace。"""
        token = secrets.token_urlsafe()
        self._tokens[token] = call_id
        return token

    def verify(self, token: str) -> str | None:
        return self._tokens.get(token)

    def revoke(self, call_id: str) -> None:
        self._tokens = {t: c for t, c in self._tokens.items() if c != call_id}


class LineSerializer(TwilioFrameSerializer):
    """TwilioFrameSerializer 子类：收到 stop 时推出 EndFrame（父类返回 None，inv-1 §1.5）。
    构造时 params=TwilioFrameSerializer.InputParams(sample_rate=engine.audio.pipeline_in_sample_rate,
    auto_hang_up=False)，不需要 account_sid/auth_token（inv-1 §1.5、S0 V5）。"""

    async def deserialize(self, data: str | bytes) -> Any:
        # todo(D): 先用 line_protocol.parse_line_message(data, direction="to_engine")（第 3 步已写好，A-23）判 stop → EndFrame；
        #          其余交给 super().deserialize(data)。
        msg = parse_line_message(data, direction="to_engine")
        if isinstance(msg, StopMsg):
            return EndFrame()
        return await super().deserialize(data)


async def line_endpoint(websocket: Any) -> None:
    """运营商回连：`{WAITLESS_LINE_WS_URL}?token=<每通令牌>`。"""
    await websocket.accept()
    services = websocket.app.state.services
    call_id = services.line_tokens.verify(websocket.query_params.get("token", ""))
    if call_id is None:
        await websocket.close(code=WS_CLOSE_UNAUTHORIZED)
        return
    try:
        try:
            connected = parse_line_message(await _handshake_text(websocket), direction="to_engine")
            if not isinstance(connected, ConnectedMsg):
                raise LineProtocolError("expected connected first")
            start = parse_line_message(await _handshake_text(websocket), direction="to_engine")
            if not isinstance(start, StartMsg):
                raise LineProtocolError("expected start second")
        except LineProtocolError:
            await websocket.close(code=WS_1002_PROTOCOL_ERROR)
            return
        if start.start.customParameters.call_id != call_id:
            await websocket.close(code=WS_CLOSE_UNAUTHORIZED)
            return
        session = services.calls.get_session(call_id)
        if session is None:
            await websocket.close(code=WS_CLOSE_UNAUTHORIZED)
            return
        await session.attach_line(websocket, start)
    except WebSocketDisconnect:
        return


async def _handshake_text(websocket: Any) -> str:
    message = await websocket.receive()
    if message["type"] == "websocket.disconnect":
        raise WebSocketDisconnect(message.get("code", 1000))
    if not isinstance(message.get("text"), str):
        raise LineProtocolError("expected a text handshake frame")
    return message["text"]
