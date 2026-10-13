"""`/ws/line` 端点、每通线路令牌、stop → EndFrame 的 serializer（D 线；tech §6.3；AC-61、AC-62；K5）。

公开接口（§7）：line_endpoint(websocket)、LineTokens.issue()/verify()、LineSerializer。
"""

from typing import Any

from pipecat.serializers.twilio import TwilioFrameSerializer

from waitless.interfaces import Clock

__all__ = ["LineSerializer", "LineTokens", "line_endpoint"]


class LineTokens:
    """实现 interfaces.LineTokenIssuer。拨号时签发（LiveCall.start），握手时校验；通话结束即作废。"""

    def __init__(self, clock: Clock) -> None:
        raise NotImplementedError

    def issue(self, call_id: str) -> str:
        """secrets.token_urlsafe；只存哈希或在内存里，不写日志、不进 trace。"""
        raise NotImplementedError

    def verify(self, token: str) -> str | None:
        raise NotImplementedError

    def revoke(self, call_id: str) -> None:
        raise NotImplementedError


class LineSerializer(TwilioFrameSerializer):
    """TwilioFrameSerializer 子类：收到 stop 时推出 EndFrame（父类返回 None，inv-1 §1.5）。
    构造时 params=TwilioFrameSerializer.InputParams(sample_rate=engine.audio.pipeline_in_sample_rate,
    auto_hang_up=False)，不需要 account_sid/auth_token（inv-1 §1.5、S0 V5）。"""

    async def deserialize(self, data: str | bytes) -> Any:
        # todo(D): 先用 line_protocol.parse_line_message(data, direction="to_engine")（第 3 步已写好，A-23）判 stop → EndFrame；
        #          其余交给 super().deserialize(data)。
        raise NotImplementedError


async def line_endpoint(websocket: Any) -> None:
    """运营商回连：`{WAITLESS_LINE_WS_URL}?token=<每通令牌>`。"""
    # todo(D): 缺口 E-G-6（定稿 endpoint_accepts，line_endpoint 同样做）：先 accept，再校验令牌；
    #          无效 → websocket.close(code=models.WS_CLOSE_UNAUTHORIZED)（4401，AC-62），不建管线。
    # todo(D): 读 connected 与 start（line_protocol 解析）→ customParameters.call_id 与令牌的 call_id 一致；
    #          把已 accept 的连接交给 session.attach_line，session 不再 accept。
    # todo(D): services = websocket.app.state.services（interfaces.AppServices）；
    #          session = services.calls.get_session(call_id)；await session.attach_line(websocket, start)。
    # todo(D): 连接关闭或 stop 时结束管线，不留悬挂任务（K5）。
    raise NotImplementedError
