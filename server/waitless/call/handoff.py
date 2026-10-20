"""`/ws/handoff/{handoff_id}`、一次性令牌与音频桥（D 线；tech §6.4；AC-63、AC-76、AC-78；K7）。

公开接口（§7）：handoff_endpoint(websocket, handoff_id)、HandoffTokens、HandoffBridge。
"""

from typing import Any, Literal
from collections import deque
from pydantic import BaseModel, ConfigDict, Field, ValidationError

from waitless.interfaces import Clock, ToolFailure
from waitless.models import (HandoffHello, HandoffReady, HandoffEnded, JoinMedia,
    HANDOFF_FRAME_BYTES, WS_CLOSE_UNAUTHORIZED)
from datetime import datetime, timedelta
import secrets
import json
from starlette.websockets import WebSocketDisconnect
from starlette.status import WS_1002_PROTOCOL_ERROR

__all__ = ["HandoffBridge", "HandoffTokens", "handoff_endpoint"]


class _HandoffProtocolError(ValueError):
    pass


class _UserDtmf(BaseModel):
    model_config = ConfigDict(extra="forbid", hide_input_in_errors=True)
    type: Literal["dtmf"]
    keys: str = Field(strict=True, pattern=r"^[0-9*#]{1,16}$")


class HandoffTokens:
    """实现 interfaces.HandoffTokenIssuer：TTL = engine.handoff.token_ttl_s，用过即作废（AC-63）。
    text-sim/replay 的 TextCall 也用它（E 线导入）。"""

    def __init__(self, clock: Clock, *, ttl_s: int) -> None:
        self.clock, self.ttl_s = clock, ttl_s
        if ttl_s <= 0:
            raise ValueError("token TTL must be positive")
        self._tokens = {}

    def issue(self, call_id: str, handoff_id: str) -> tuple[str, str]:
        token = secrets.token_urlsafe(32)
        expires_ms = self.clock.monotonic_ms() + self.ttl_s * 1000
        expiry = datetime.fromisoformat(self.clock.now_iso()) + timedelta(seconds=self.ttl_s)
        self._tokens[(handoff_id, token)] = (call_id, expires_ms)
        return token, expiry.isoformat()

    def revoke(self, call_id: str) -> None:
        self._tokens = {key: value for key, value in self._tokens.items() if value[0] != call_id}

    def consume(self, handoff_id: str, token: str) -> str | None:
        record = self._tokens.pop((handoff_id, token), None)
        if record is None or self.clock.monotonic_ms() >= record[1]:
            return None
        return record[0]


class HandoffBridge:
    """同一通电话里，把对方音频转给浏览器、把浏览器麦克风送进线路；不重拨、不新建线路（AC-76）。"""

    def __init__(self, *, websocket: Any, media_gate: Any, company_name: str) -> None:
        self.websocket, self.media_gate, self.company_name = websocket, media_gate, company_name
        self._ready = False
        self._closed = False
        self._close_reason = None
        self._user_key_window: deque[tuple[int, int]] = deque()

    async def run(self) -> str:
        """收 hello（models.HandoffHello）→ 发 ready（models.HandoffReady）→ 双向转发 20 ms PCM16 16 kHz 帧，
        直到 return_to_agent 或连接断开；返回结束原因（returned / company_hangup / disconnected）。"""
        try:
            hello = await self.websocket.receive()
            if hello["type"] == "websocket.disconnect":
                return self._close_reason or "disconnected"
            if not isinstance(hello.get("text"), str):
                raise _HandoffProtocolError("expected a text hello frame")
            if len(hello["text"].encode("utf-8")) > 256:
                raise _HandoffProtocolError("handoff text frame too large")
            try:
                HandoffHello.model_validate_json(hello["text"])
            except ValueError:
                raise _HandoffProtocolError("invalid handoff hello") from None
            before = getattr(self, 'before_ready', None)
            if before is not None and not await before():
                return 'disconnected'
            cleaned = getattr(self.media_gate, '_cleaned', None)
            if cleaned is not None:
                await cleaned.wait()
            await self.websocket.send_json(HandoffReady(type="ready", company_name=self.company_name,
                media=JoinMedia(ai_media="disconnected", recording=False, transcription=False)).model_dump(mode="json"))
            ready = getattr(self, 'on_ready', None)
            if ready is not None and not await ready():
                return 'disconnected'
            self._ready = True
            while not self._closed:
                message = await self.websocket.receive()
                if message["type"] == "websocket.disconnect":
                    return self._close_reason or "disconnected"
                if message.get("bytes") is not None:
                    audio = message["bytes"]
                    if len(audio) != HANDOFF_FRAME_BYTES:
                        raise _HandoffProtocolError("invalid handoff PCM frame length")
                    await self.media_gate.outbound.push_user_audio(audio)
                elif message.get("text") is not None:
                    if len(message["text"].encode("utf-8")) > 256:
                        raise _HandoffProtocolError("handoff text frame too large")
                    try:
                        value = json.loads(message["text"])
                    except ValueError:
                        raise _HandoffProtocolError("invalid handoff control message") from None
                    if value == {"type": "return_to_agent"}:
                        return 'user_hangup' if getattr(self, 'live_no_resume', False) else 'returned' 
                    if isinstance(value, dict) and value.get("type") == "dtmf":
                        await self._user_dtmf(value)
            return self._close_reason or "returned"
        except WebSocketDisconnect:
            return self._close_reason or "disconnected"
        except _HandoffProtocolError:
            try:
                await self.websocket.close(code=WS_1002_PROTOCOL_ERROR)
            except (WebSocketDisconnect, RuntimeError):
                pass
            return "disconnected"
        finally:
            self._ready = False

    async def _user_dtmf(self, value: dict) -> None:
        try:
            message = _UserDtmf.model_validate(value)
        except ValidationError:
            await self.websocket.send_json({"type": "dtmf_error", "error": "invalid_keys"})
            return
        now = self.media_gate._clock.monotonic_ms()
        while self._user_key_window and self._user_key_window[0][0] <= now - 10_000:
            self._user_key_window.popleft()
        count = len(message.keys)
        error = None
        if sum(size for _, size in self._user_key_window) + count > 32:
            error = "rate_limited"
        elif not self.media_gate.in_handoff:
            error = "not_in_handoff"
        elif not await self.media_gate.outbound.push_user_dtmf(message.keys):
            error = "not_in_handoff"
        if error:
            await self.websocket.send_json({"type": "dtmf_error", "error": error})
            return
        self._user_key_window.append((now, count))
        await self.websocket.send_json({"type": "dtmf_ack", "count": count})

    async def to_browser(self, pcm16_16k: bytes) -> None:
        """对方音频取自管线输入（serializer 已转成 16 kHz）。"""
        if self._ready and not self._closed:
            try:
                await self.websocket.send_bytes(pcm16_16k)
            except (WebSocketDisconnect, RuntimeError):
                self._ready = False

    async def close(self, reason: str) -> None:
        """发 {"type":"ended","reason":...}（returned 或 company_hangup）后关闭。"""
        if not self._closed:
            self._closed = True
            self._close_reason = reason
            self._ready = False
            try:
                await self.websocket.send_json(HandoffEnded(type="ended", reason=reason).model_dump(mode="json"))
                await self.websocket.close()
            except (WebSocketDisconnect, RuntimeError):
                pass


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
    services = websocket.app.state.services
    await websocket.accept()
    call_id = services.handoff_tokens.consume(handoff_id, websocket.query_params.get("token", ""))
    if call_id is None:
        await websocket.close(code=WS_CLOSE_UNAUTHORIZED)
        return
    session = services.calls.get_session(call_id)
    if session is None:
        await websocket.close(code=WS_CLOSE_UNAUTHORIZED)
        return
    await session.attach_handoff(websocket, handoff_id)
    if getattr(getattr(session, 'spec', None), 'mode', None) in ('live', 'live-sim'):
        return
    if session.status().phase == "in_handoff":
        await services.clock.sleep(services.settings.engine.handoff.resume_grace_ms / 1000)
        if session.status().phase == "in_handoff":
            try:
                await session.return_to_agent(handoff_id)
            except ToolFailure as exc:
                if exc.code != "no_active_handoff":
                    raise
