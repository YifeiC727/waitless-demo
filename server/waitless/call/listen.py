"""Read-only, in-memory line audio mixing and single-listener streaming."""

import asyncio
import json
import sys
from array import array
from dataclasses import dataclass, field
from typing import Any

from pydantic import AwareDatetime, BaseModel, ConfigDict, model_validator
from starlette.websockets import WebSocketDisconnect

from waitless.models import HANDOFF_FRAME_BYTES, HANDOFF_SAMPLE_RATE, WS_CLOSE_UNAUTHORIZED

LISTEN_MAX_LAG_MS = 1000
LISTEN_QUEUE_FRAMES = 25


class ListenMixer:
    """Use inbound PCM as the clock; retain at most max_lag_ms of outbound PCM."""

    def __init__(self, *, sample_rate: int, frame_bytes: int, max_lag_ms: int) -> None:
        if sample_rate <= 0 or frame_bytes <= 0 or frame_bytes % 2 or max_lag_ms < 0:
            raise ValueError("invalid PCM mixer settings")
        self.sample_rate, self.frame_bytes = sample_rate, frame_bytes
        self._max_out_bytes = int(sample_rate * max_lag_ms / 1000) * 2
        self._in = bytearray()
        self._out = bytearray()
        self.dropped_out_bytes = 0

    @property
    def out_buffered_ms(self) -> float:
        return len(self._out) * 1000 / (self.sample_rate * 2)

    def add_line_out(self, pcm: bytes) -> None:
        self._out.extend(pcm)
        excess = len(self._out) - self._max_out_bytes
        if excess > 0:
            # Keep PCM sample boundaries even if a chunk ends mid-sample.
            excess = min(len(self._out), excess + excess % 2)
            del self._out[:excess]
            self.dropped_out_bytes += excess

    def add_line_in(self, pcm: bytes) -> list[bytes]:
        self._in.extend(pcm)
        frames = []
        while len(self._in) >= self.frame_bytes:
            incoming = array('h', self._in[:self.frame_bytes])
            available = min(self.frame_bytes, len(self._out) // 2 * 2)
            outgoing = array('h', self._out[:available])
            del self._in[:self.frame_bytes]
            del self._out[:available]
            if sys.byteorder != 'little':
                incoming.byteswap()
                outgoing.byteswap()
            for i, value in enumerate(outgoing):
                incoming[i] = max(-32768, min(32767, incoming[i] + value))
            if sys.byteorder != 'little':
                incoming.byteswap()
            frames.append(incoming.tobytes())
        return frames

    def clear_out(self) -> None:
        self._out.clear()


@dataclass
class _Listener:
    websocket: Any
    queue: asyncio.Queue = field(default_factory=lambda: asyncio.Queue(LISTEN_QUEUE_FRAMES))
    stop: asyncio.Event = field(default_factory=asyncio.Event)
    done: asyncio.Event = field(default_factory=asyncio.Event)
    reason: str | None = None
    close_code: int = 1000

    def end(self, reason: str, code: int = 1000) -> None:
        if self.reason is None:
            self.reason, self.close_code = reason, code
            self.stop.set()


class ListenHub:
    """Synchronous pipeline taps; all WebSocket I/O belongs to attach's tasks."""

    def __init__(self, *, media_gate: Any, sample_rate: int) -> None:
        self.media_gate, self.sample_rate = media_gate, sample_rate
        self._mixer = self._new_mixer()
        self._queue = asyncio.Queue(LISTEN_QUEUE_FRAMES)
        self._listener: _Listener | None = None
        self._attach_lock = asyncio.Lock()
        self._closed_reason: str | None = None
        self.dropped_in_bytes = 0
        self.dropped_out_bytes = 0

    def _new_mixer(self) -> ListenMixer:
        return ListenMixer(sample_rate=self.sample_rate, frame_bytes=HANDOFF_FRAME_BYTES,
                           max_lag_ms=LISTEN_MAX_LAG_MS)

    def _can_tap(self, pcm: bytes, sample_rate: int, *, outbound: bool) -> bool:
        if self.media_gate is not None and self.media_gate.in_handoff:
            if self._listener is not None:
                self._listener.end('handoff')
            self._mixer = self._new_mixer()
            return False
        if sample_rate != self.sample_rate:
            if outbound:
                self.dropped_out_bytes += len(pcm)
            else:
                self.dropped_in_bytes += len(pcm)
            return False
        return (self._closed_reason is None and self._listener is not None
                and self._listener.reason is None)

    def on_line_in(self, pcm: bytes, sample_rate: int) -> None:
        if not self._can_tap(pcm, sample_rate, outbound=False):
            return
        for frame in self._mixer.add_line_in(pcm):
            if self._queue.full():
                self._queue.get_nowait()
            self._queue.put_nowait(frame)

    def on_line_out(self, pcm: bytes, sample_rate: int) -> None:
        if self._can_tap(pcm, sample_rate, outbound=True):
            before = self._mixer.dropped_out_bytes
            self._mixer.add_line_out(pcm)
            self.dropped_out_bytes += self._mixer.dropped_out_bytes - before

    def on_clear(self) -> None:
        self._mixer.clear_out()
        # MediaGate broadcasts an interruption as it enters handoff.
        if self.media_gate is not None and self.media_gate.in_handoff:
            if self._listener is not None:
                self._listener.end('handoff')

    async def _send(self, listener: _Listener) -> None:
        await listener.websocket.send_json(dict(type='ready', format='pcm_s16le',
                                               sample_rate=HANDOFF_SAMPLE_RATE, channels=1))
        while not listener.stop.is_set():
            frame = await listener.queue.get()
            if self.media_gate is not None and self.media_gate.in_handoff:
                listener.end('handoff')
                return
            if not listener.stop.is_set():
                await listener.websocket.send_bytes(frame)

    async def _receive(self, listener: _Listener) -> None:
        while not listener.stop.is_set():
            message = await listener.websocket.receive()
            if message['type'] == 'websocket.disconnect':
                listener.end('disconnected')
                return
            if message.get('bytes') is not None:
                listener.end('disconnected', 1002)
                return
            try:
                control = json.loads(message.get('text', ''))
            except ValueError:
                control = None
            if control == {'type': 'stop'}:
                listener.end('stopped')
            else:
                listener.end('disconnected', 1002)

    async def attach(self, websocket: Any) -> str:
        async with self._attach_lock:
            if self._listener is not None:
                self._listener.end('replaced')
                await self._listener.done.wait()
            if self._closed_reason is not None:
                await websocket.close(code=WS_CLOSE_UNAUTHORIZED)
                return self._closed_reason
            if self.media_gate is not None and self.media_gate.in_handoff:
                await websocket.close(code=WS_CLOSE_UNAUTHORIZED)
                return 'handoff'
            listener = _Listener(websocket)
            self._listener, self._queue = listener, listener.queue
            self._mixer = self._new_mixer()
        tasks = [asyncio.create_task(self._send(listener)),
                 asyncio.create_task(self._receive(listener)),
                 asyncio.create_task(listener.stop.wait())]
        try:
            finished, _ = await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)
            for task in finished:
                task.result()
        except (WebSocketDisconnect, RuntimeError):
            listener.end('disconnected')
        finally:
            listener.end('disconnected')
            for task in tasks:
                task.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)
            try:
                if listener.reason != 'disconnected':
                    await websocket.send_json({'type': 'ended', 'reason': listener.reason})
                await websocket.close(code=listener.close_code)
            except (WebSocketDisconnect, RuntimeError):
                pass
            finally:
                if self._listener is listener:
                    self._listener = None
                    self._mixer = self._new_mixer()
                while not listener.queue.empty():
                    listener.queue.get_nowait()
                listener.done.set()
        return listener.reason

    async def close(self, reason: str) -> None:
        if self._closed_reason is None:
            self._closed_reason = reason
        listener = self._listener
        if listener is not None:
            listener.end(self._closed_reason)
            await listener.done.wait()


class ListenTicketOut(BaseModel):
    model_config = ConfigDict(extra='forbid')

    task_id: str
    call_id: str
    ws_path: str
    token: str
    expires_at: AwareDatetime

    @model_validator(mode='after')
    def _path_matches(self) -> 'ListenTicketOut':
        if self.ws_path != f'/ws/listen/{self.call_id}':
            raise ValueError('ws_path must be /ws/listen/{call_id}')
        return self


async def listen_endpoint(websocket: Any, call_id: str) -> None:
    await websocket.accept()
    origins = getattr(websocket.app.state, 'listen_origins', ())
    if websocket.headers.get('origin') not in origins:
        await websocket.close(code=WS_CLOSE_UNAUTHORIZED)
        return
    session = websocket.app.state.services.calls.get_session(call_id)
    consume = getattr(session, 'consume_listen_token', None)
    if not callable(consume) or not consume(websocket.query_params.get('token', '')):
        await websocket.close(code=WS_CLOSE_UNAUTHORIZED)
        return
    await session.attach_listen(websocket)
