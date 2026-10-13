"""`/ws/handoff/{handoff_id}`、一次性令牌与音频桥（D 线；tech §6.4；AC-63、AC-76、AC-78；K7）。

公开接口（§7）：handoff_endpoint(websocket, handoff_id)、HandoffTokens、HandoffBridge。
"""

from typing import Any

from waitless.interfaces import Clock

__all__ = ["HandoffBridge", "HandoffTokens", "handoff_endpoint"]


class HandoffTokens:
    """实现 interfaces.HandoffTokenIssuer：TTL = engine.handoff.token_ttl_s，用过即作废（AC-63）。
    text-sim/replay 的 TextCall 也用它（E 线导入）。"""

    def __init__(self, clock: Clock, *, ttl_s: int) -> None:
        raise NotImplementedError

    def issue(self, call_id: str, handoff_id: str) -> tuple[str, str]:
        raise NotImplementedError

    def consume(self, handoff_id: str, token: str) -> str | None:
        raise NotImplementedError


class HandoffBridge:
    """同一通电话里，把对方音频转给浏览器、把浏览器麦克风送进线路；不重拨、不新建线路（AC-76）。"""

    def __init__(self, *, websocket: Any, media_gate: Any, company_name: str) -> None:
        raise NotImplementedError

    async def run(self) -> str:
        """收 hello（models.HandoffHello）→ 发 ready（models.HandoffReady）→ 双向转发 20 ms PCM16 16 kHz 帧，
        直到 return_to_agent 或连接断开；返回结束原因（returned / company_hangup / disconnected）。"""
        raise NotImplementedError

    async def to_browser(self, pcm16_16k: bytes) -> None:
        """对方音频取自管线输入（serializer 已转成 16 kHz）。"""
        raise NotImplementedError

    async def close(self, reason: str) -> None:
        """发 {"type":"ended","reason":...}（returned 或 company_hangup）后关闭。"""
        raise NotImplementedError


async def handoff_endpoint(websocket: Any, handoff_id: str) -> None:
    """路径 /ws/handoff/{handoff_id}?token=<join_call 返回的一次性令牌>（浏览器用与页面同一 host 的 wss）。

    缺口 D-G-5（定稿 check_phase）：attach_handoff 返回后，看 session.status().phase——仍是 in_handoff 才等
    engine.handoff.resume_grace_ms；到时再看一次，仍是 in_handoff 就 await session.return_to_agent(handoff_id)，
    遇到 ToolFailure("no_active_handoff")（用户恰好点了 Return）当作已接回。不改 attach_handoff 的返回值。
    转接事件的顺序（D-G-4）：handoff_announced → handoff_ready → handoff_started → … → handoff_ended；
    本端点不写 trace，handoff_ended 由 session.return_to_agent 写（D-G-3）。
    """
    # todo(D): 缺口 E-G-6（定稿 endpoint_accepts）：先 await websocket.accept()，再
    #          call_id = services.handoff_tokens.consume(handoff_id, token)（services = websocket.app.state.services）；
    #          None → close(code=models.WS_CLOSE_UNAUTHORIZED)（4401，过期或重复使用，AC-63）。session 不再 accept。
    # todo(D): session = services.calls.get_session(call_id)；await session.attach_handoff(websocket, handoff_id)，
    #          它在连接关闭时返回（live 与 text 一样）。
    # todo(D): 缺口 E-G-5（定稿 endpoint_owns_timer）+ D-G-5（定稿 check_phase）：attach_handoff 返回后按上面的 phase
    #          规则判断是否断线；断线就等 engine.handoff.resume_grace_ms（用 services.clock），再 await
    #          session.return_to_agent(handoff_id)（session 内部做 exit_handoff + 写 handoff_ended + 注入 "handoff.resume"，
    #          phase=resuming，AC-78）。live 与 text 共用这一处。
    raise NotImplementedError
