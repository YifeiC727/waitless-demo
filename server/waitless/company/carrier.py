"""模拟运营商：拨号 API、场景切换、媒体中继（B 线；tech §6.3；AC-83、AC-84、AC-98）。

公开接口（§7）：carrier 路由（§6.3 表）、CarrierCall。只监听回环（公司进程的 host/port 来自 Settings）。
"""

import asyncio
import contextlib
import json
import logging
import threading
from collections import deque
from pathlib import Path
from typing import Any
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import JSONResponse
from websockets.exceptions import ConnectionClosed

from waitless.call.line_protocol import (
    LINE_FRAME_MS, MULAW_FRAME_BYTES, ClearMsg, MediaMsg, MarkMsg,
    encode_line_message, make_connected, make_media_in, make_start, make_stop, media_payload_bytes,
    new_call_sid, new_stream_sid, parse_line_message,
)
from waitless.company.config import ScenarioError, list_scenarios, load_scenario
from waitless.company.engine import ScenarioEngine, _live_scenario
from waitless.company.scripted import ScriptedTurns, check_scripted_steps
from waitless.company.dtmf_detect import GoertzelDetector
from waitless.company.ivr_audio import PromptCache, hold_music
from waitless.audio.codec import pcm16_rms_dbfs, ulaw_decode, ulaw_encode

from waitless.models import ApiError, CarrierDialIn, CarrierDialOut, CarrierHangupOut, CompanyConfig, ScenarioSelection

__all__ = ["CarrierCall", "CarrierState", "build_carrier_router"]


class CarrierCall:
    """一通模拟电话：连引擎的 /ws/line（stream_url?token=stream_token），把媒体在引擎与公司方之间中继。"""

    def __init__(self, *, dial: CarrierDialIn, call_sid: str, scenario_id: str, company: CompanyConfig) -> None:
        self.dial = dial
        self.call_sid = call_sid
        self.scenario_id = scenario_id
        self.company = company
        self._agent_mode = "sonic"
        self._scenario = None
        self._engine: ScenarioEngine | None = None
        self._prompt_cache: PromptCache | None = None
        self._voice_cache: Any = None
        self._engine_config: Any = None
        self._scripted_turns: ScriptedTurns | None = None
        self._sample_rate = MULAW_FRAME_BYTES * 1000 // LINE_FRAME_MS
        self._stats_path: Path | None = None
        self._stream_sid = new_stream_sid()
        self._sequence = 0
        self._ws: Any = None
        self._closed = False
        self._started = False
        self._hangup_lock = asyncio.Lock()
        self._marks: list[str] = []
        self._nonsilent_inbound_frames = 0
        # All times use the process monotonic clock (seconds), shared with the
        # same-host probe. Keep every rep-segment arrival: only the probe knows
        # the trusted holder-ready boundary; first voice is never that boundary.
        self._rep_first_audio_at: float | None = None
        self._rep_last_audio_at: float | None = None
        self._rep_first_line_end_at: float | None = None
        self._rep_first_line: Any = None
        self._nonsilent_inbound_timestamps_during_rep: list[float] = []
        self._bot_endpoint: str | None = None
        self._bot_ws: Any = None
        self._bot_receive: asyncio.Task | None = None
        self._bot_step: str | None = None
        self._bot_audio: deque[bytes] = deque()
        self._bot_sequence = 0
        self._bot_chunk = 0
        self._bot_last_input = 0.0
        self._bot_threshold_dbfs: float | None = None

    async def run(self) -> None:
        """发 connected、start（customParameters={call_id, task_id}）、20 ms μ-law media；收引擎的 media/mark/clear
        并按 line_protocol 校验；DTMF 用 company.dtmf_detect.GoertzelDetector 识别后交场景引擎。
        帧一律用 call/line_protocol.py 的现成函数构造与解析（第 3 步已写好，A-23）：make_connected、make_start、
        make_media_in、make_stop、encode_line_message；收帧 parse_line_message(text, direction="to_carrier")。
        μ-law 与 PCM16 互转用 audio/codec.py 的 ulaw_encode、ulaw_decode（第 3 步已写好，A-21）。"""
        if self._scenario is None:
            raise ScenarioError("CarrierCall needs a loaded scenario before running")
        self._scenario = _live_scenario(self._scenario)
        if self._closed:
            return
        from websockets.asyncio.client import connect
        parts = urlsplit(self.dial.stream_url)
        query = [(k, v) for k, v in parse_qsl(parts.query, keep_blank_values=True) if k != "token"]
        query.append(("token", self.dial.stream_token))
        url = urlunsplit(parts._replace(query=urlencode(query)))
        self._engine = engine = ScenarioEngine(self._scenario, self.company)
        pending = deque(engine.start())
        detector = GoertzelDetector(sample_rate=self._sample_rate)
        receive = prepare = None
        playing = None
        audio = b""
        offset = chunk = 0
        frame_s = LINE_FRAME_MS / 1000
        menu_deadline = None
        holder_choice_blocked = False
        try:
            ws = await connect(url, proxy=None)
            async with self._hangup_lock:
                self._ws = ws
                if self._closed:
                    await ws.close()
                    return
                await ws.send(encode_line_message(make_connected()))
                self._sequence += 1
                await ws.send(encode_line_message(make_start(
                    sequence=self._sequence, stream_sid=self._stream_sid,
                    call_sid=self.call_sid, call_id=self.dial.call_id, task_id=self.dial.task_id)))
                self._started = True
            loop = asyncio.get_running_loop()
            call_deadline = loop.time() + self._scenario.max_call_seconds
            if self._prompt_cache is None:
                # Standalone CarrierState / text-sim uses only the control lifecycle.
                await self._wait_control_line(ws, engine, call_deadline)
                return
            turns = None
            if self._agent_mode == "scripted":
                if self._engine_config is None or self._voice_cache is None:
                    raise ScenarioError("Scripted CarrierCall needs engine configuration and voice caches")
                self._scripted_turns = turns = ScriptedTurns(
                    engine=engine, scenario=self._scenario, company=self.company,
                    engine_config=self._engine_config)
            next_frame = loop.time()
            receive = asyncio.create_task(self._receive_line(ws))
            while not self._closed:
                now = loop.time()
                if now >= call_deadline:
                    break
                if turns is None and self._bot_ws is not None and (
                        engine.ended or self._bot_step != engine.current_step):
                    # The bot drains its output before closing. Play the relay
                    # queue too before hanging up or opening the next role.
                    if self._bot_receive is not None and self._bot_receive.done():
                        self._bot_receive.result()
                        if not self._bot_audio and now >= next_frame:
                            await self._close_bot()
                if turns is not None:
                    pending.extend(turns.poll(int(now * 1000)))
                if menu_deadline is not None and now >= menu_deadline:
                    pending.extend(engine.on_timeout())
                    menu_deadline = None
                if prepare is not None and prepare.done():
                    audio = prepare.result()
                    prepare = None
                    offset = 0
                if not audio and prepare is None:
                    if playing is not None:
                        if playing.kind == "menu" and not engine.ended:
                            menu_deadline = now + self.company.menu.timeout_sec
                        if turns is not None:
                            turns.line_finished(playing, int(now * 1000))
                        playing = None
                    while pending and (pending[0].kind == "hangup"
                                       or (turns is None and pending[0].kind == "speech")):
                        # Speech is produced by Sonic for the live scenario step.
                        pending.popleft()
                    if pending:
                        playing = pending.popleft()
                        if turns is not None:
                            logging.getLogger(__name__).info(
                                "scripted line step=%s intent=%s voice=%s", playing.step_id,
                                playing.expect.intent if playing.expect else None, turns.voice_for(playing))
                        prepare = asyncio.create_task(asyncio.to_thread(self._line_audio, playing))
                    elif engine.ended and self._bot_ws is None:
                        break
                if turns is None and not audio and prepare is None and not pending and not engine.ended:
                    step = self._scenario.steps[engine.current_step]
                    if step.kind in {"ai_agent", "live_rep"} and self._bot_ws is None:
                        await self._open_bot("agent" if step.kind == "ai_agent" else "rep")
                    if self._bot_receive is not None and self._bot_receive.done():
                        self._bot_receive.result()
                        if not engine.ended and self._bot_step == engine.current_step:
                            raise ScenarioError("Company bot disconnected before completing its step")
                if now >= next_frame:
                    if (self._bot_ws is not None and not engine.ended
                            and self._bot_step == engine.current_step
                            and now - self._bot_last_input >= 3 * frame_s):
                        await self._send_bot_media(bytes([255]) * MULAW_FRAME_BYTES)
                    payload = audio[offset:offset + MULAW_FRAME_BYTES] if audio else b""
                    rep_audio = bool(audio and playing is not None and playing.role == "rep")
                    bot_rep_audio = False
                    if self._bot_audio:
                        payload = self._bot_audio.popleft()
                        bot_rep_audio = (self._bot_step is not None
                                         and self._scenario.steps[self._bot_step].kind == "live_rep")
                    payload = payload.ljust(MULAW_FRAME_BYTES, b"\xff")
                    async with self._hangup_lock:
                        if self._closed:
                            break
                        if audio and offset == 0 and turns is not None and playing is not None:
                            was_open = engine._holder_prompt_open
                            turns.line_started(playing)
                            if not was_open and engine._holder_prompt_open:
                                # A candidate already held before the question's first
                                # audio frame cannot become a new choice after it starts.
                                holder_choice_blocked = detector.tone_present
                        chunk += 1
                        self._sequence += 1
                        await ws.send(encode_line_message(make_media_in(
                            sequence=self._sequence, stream_sid=self._stream_sid, chunk=chunk,
                            timestamp_ms=(chunk - 1) * LINE_FRAME_MS, ulaw=payload)))
                        # A synthesis/log timestamp cannot establish a wire
                        # boundary. Record only after the actual send succeeds.
                        if rep_audio or bot_rep_audio:
                            sent_at = loop.time()
                            if self._rep_first_audio_at is None:
                                self._rep_first_audio_at = sent_at
                                self._rep_first_line = playing if rep_audio else None
                            self._rep_last_audio_at = sent_at
                            if (rep_audio and playing is self._rep_first_line
                                    and offset + MULAW_FRAME_BYTES >= len(audio)):
                                duration = min(MULAW_FRAME_BYTES, len(audio) - offset) / self._sample_rate
                                self._rep_first_line_end_at = sent_at + duration
                    if audio:
                        offset += MULAW_FRAME_BYTES
                        if offset >= len(audio):
                            audio = b""
                    # Do not send a burst of overdue frames after a slow operation.
                    next_frame = max(next_frame + frame_s, loop.time())
                deadlines = [next_frame, call_deadline]
                if menu_deadline is not None:
                    deadlines.append(menu_deadline)
                watched = {receive}
                if prepare is not None:
                    watched.add(prepare)
                done, _ = await asyncio.wait(watched, timeout=max(0, min(deadlines) - loop.time()),
                                             return_when=asyncio.FIRST_COMPLETED)
                if receive in done:
                    raw, received_at = receive.result()
                    msg = parse_line_message(raw, direction="to_carrier")
                    if msg.streamSid != self._stream_sid:
                        raise ValueError("Line message stream identifier differs from this call")
                    if isinstance(msg, MediaMsg):
                        pcm = ulaw_decode(media_payload_bytes(msg))
                        self._record_rep_inbound(pcm, received_at)
                    if isinstance(msg, MarkMsg):
                        self._marks.append(msg.mark.name)
                    elif isinstance(msg, ClearMsg):
                        detector.reset()
                    elif isinstance(msg, MediaMsg) and self._bot_ws is not None:
                        if engine._bot_holder_requested and not engine._bot_holder_verified:
                            if pcm16_rms_dbfs(pcm) > self._bot_threshold_dbfs:
                                self._nonsilent_inbound_frames += 1
                        if not engine.ended and self._bot_step == engine.current_step:
                            self._bot_last_input = loop.time()
                            await self._send_bot_media(media_payload_bytes(msg))
                    elif isinstance(msg, MediaMsg):
                        detected = detector.feed(pcm)
                        if not detector.tone_present:
                            holder_choice_blocked = False
                        for digit in detected:
                            holder_window = turns is not None and turns.in_handoff_window
                            if holder_window and (not turns.counts_holder_audio or holder_choice_blocked):
                                continue
                            prior_line = engine._pending if turns is not None else None
                            lines = engine.on_digits(digit)
                            if holder_window:
                                turns.holder_keypad_lines(lines)
                            if lines:
                                if turns is not None:
                                    if not holder_window:
                                        turns.lines_replaced()
                                    if not holder_window and prior_line is not None:
                                        logging.getLogger(__name__).info("keypad entry %s",
                                            "rejected" if lines == [prior_line] else "accepted")
                                # A key interrupts the prior prompt and resets the menu timer.
                                pending.clear()
                                if prepare is not None:
                                    prepare.cancel()
                                    await asyncio.gather(prepare, return_exceptions=True)
                                    prepare = None
                                audio, playing = b"", None
                                menu_deadline = None
                                pending.extend(lines)
                        if turns is not None:
                            now_ms = int(loop.time() * 1000)
                            if detector.tone_present:
                                # A trailing automatic-service key can overlap
                                # rep playback. It must never stand in for the
                                # holder's first spoken answer and open a window.
                                turns.feed_keypad_tone(now_ms)
                                voiced = False
                            else:
                                voiced = turns.feed(pcm, now_ms)
                            if turns.counts_holder_audio and voiced:
                                self._nonsilent_inbound_frames += 1
                    receive = asyncio.create_task(self._receive_line(ws))
        except ConnectionClosed:
            # A normally closed peer still takes the same local cleanup path.
            pass
        finally:
            for task in (receive, prepare):
                if task is not None:
                    task.cancel()
            await asyncio.gather(*(t for t in (receive, prepare) if t is not None), return_exceptions=True)
            await self.hangup()

    async def _receive_line(self, ws: Any) -> tuple[str, float]:
        raw = await ws.recv()
        # Capture arrival before synthesis/playback or digit handling can defer
        # processing this completed receive task across the rep boundary.
        return raw, asyncio.get_running_loop().time()

    def _record_rep_inbound(self, pcm: bytes, received_at: float) -> None:
        """Count decoded voiced media before any scripted/digit processing.

        Tone frames are included if voiced: this measures wire energy, not
        speech recognition or join state. Neither PCM nor key values survive.
        """
        if self._rep_first_audio_at is None or received_at < self._rep_first_audio_at:
            return
        threshold = (self._engine_config.vad.threshold_dbfs if self._engine_config is not None
                     else self._bot_threshold_dbfs)
        if threshold is not None and pcm16_rms_dbfs(pcm) > threshold:
            self._nonsilent_inbound_timestamps_during_rep.append(received_at)

    async def _open_bot(self, role: str) -> None:
        if not self._bot_endpoint:
            raise ScenarioError("Company bot WebSocket endpoint is not configured")
        from websockets.asyncio.client import connect
        parts = urlsplit(self._bot_endpoint)
        url = urlunsplit(parts._replace(query=urlencode({"role": role, "call_sid": self.call_sid})))
        ws = await connect(url, proxy=None)
        async with self._hangup_lock:
            if self._closed:
                await ws.close()
                return
            self._bot_ws = ws
            self._bot_step = self._engine.current_step
            self._bot_sequence, self._bot_chunk = 1, 0
            self._bot_last_input = asyncio.get_running_loop().time()
            await ws.send(encode_line_message(make_connected()))
            await ws.send(encode_line_message(make_start(
                sequence=self._bot_sequence, stream_sid=self._stream_sid, call_sid=self.call_sid,
                call_id=self.dial.call_id, task_id=self.dial.task_id)))
            self._bot_receive = asyncio.create_task(self._read_bot(ws))

    async def _send_bot_media(self, payload: bytes) -> None:
        self._bot_chunk += 1
        self._bot_sequence += 1
        await self._bot_ws.send(encode_line_message(make_media_in(
            sequence=self._bot_sequence, stream_sid=self._stream_sid, chunk=self._bot_chunk,
            timestamp_ms=(self._bot_chunk - 1) * LINE_FRAME_MS, ulaw=payload)))

    async def _read_bot(self, ws: Any) -> None:
        try:
            async for raw in ws:
                msg = parse_line_message(raw, direction="to_carrier")
                if msg.streamSid != self._stream_sid:
                    raise ValueError("Company bot stream identifier differs from this call")
                if isinstance(msg, MediaMsg):
                    payload = media_payload_bytes(msg)
                    if len(payload) > MULAW_FRAME_BYTES:
                        raise ValueError("Company bot audio exceeds a 20 ms line frame")
                    self._bot_audio.append(payload)
                elif isinstance(msg, ClearMsg):
                    self._bot_audio.clear()
        except ConnectionClosed:
            pass

    async def _close_bot(self) -> None:
        ws, reader = self._bot_ws, self._bot_receive
        self._bot_ws = self._bot_receive = self._bot_step = None
        # Normal step changes arrive here only after the reader and queue
        # drain; explicit hangup may discard audio to close immediately.
        self._bot_audio.clear()
        try:
            if ws is not None:
                try:
                    self._bot_sequence += 1
                    await ws.send(encode_line_message(make_stop(sequence=self._bot_sequence,
                        stream_sid=self._stream_sid, call_sid=self.call_sid)))
                except ConnectionClosed:
                    pass
                finally:
                    await ws.close()
        finally:
            if reader is not None:
                reader.cancel()
                await asyncio.gather(reader, return_exceptions=True)

    async def _wait_control_line(self, ws: Any, engine: ScenarioEngine, call_deadline: float) -> None:
        loop = asyncio.get_running_loop()
        menu_deadline = loop.time() + self.company.menu.timeout_sec
        while not self._closed:
            remaining = min(call_deadline, menu_deadline) - loop.time()
            if remaining <= 0:
                if loop.time() >= call_deadline:
                    return
                engine.on_timeout()
                if engine.ended:
                    return
                menu_deadline = loop.time() + self.company.menu.timeout_sec
                continue
            try:
                raw = await asyncio.wait_for(ws.recv(), remaining)
            except TimeoutError:
                continue
            msg = parse_line_message(raw, direction="to_carrier")
            if msg.streamSid != self._stream_sid:
                raise ValueError("Line message stream identifier differs from this call")
            if isinstance(msg, MarkMsg):
                self._marks.append(msg.mark.name)

    def _line_audio(self, line: Any) -> bytes:
        if line.kind == "hold":
            pcm = hold_music(self._scenario.steps[line.step_id].seconds, sample_rate=self._sample_rate)
        else:
            if self._prompt_cache is None:
                raise ScenarioError("CarrierCall needs a configured Polly prompt cache")
            if line.kind == "speech" and self._scripted_turns is not None:
                pcm = self._voice_cache(self._scripted_turns.voice_for(line)).get(line.text)
            else:
                pcm = self._prompt_cache.get(line.text)
        return ulaw_encode(pcm)

    async def hangup(self) -> None:
        """发 stop 并关闭连接（菜单两次超时、坐席结束或引擎挂断，AC-83）。
        结束时若 settings.company_stats_path 有值，往该文件追加一行 JSON
        {call_sid, nonsilent_inbound_frames_during_handoff}（缺口 X-G6，定稿 stats_jsonl，暂定）。
        收到引擎的 mark 只记录、不回显（缺口 B-G-10，定稿 no_echo；D 不得依赖 mark 回显）。"""
        async with self._hangup_lock:
            if self._closed:
                return
            self._closed = True
            if self._engine is not None and self._engine._handoff_joined:
                # Disconnecting a scripted holder must fail closed too.
                self._engine.on_handoff(False)
                self._engine._finish()
            if self._scripted_turns is not None:
                self._scripted_turns.lines_replaced()
            try:
                try:
                    await self._close_bot()
                finally:
                    # A failed bot teardown must not leave the outer line open.
                    if self._ws is not None:
                        try:
                            if self._started:
                                self._sequence += 1
                                await self._ws.send(encode_line_message(make_stop(
                                    sequence=self._sequence, stream_sid=self._stream_sid, call_sid=self.call_sid)))
                        except ConnectionClosed:
                            # The peer already ended the line; local cleanup still completes.
                            pass
                        finally:
                            await self._ws.close()
            finally:
                if self._stats_path is not None:
                    stats = {"call_sid": self.call_sid,
                             "nonsilent_inbound_frames_during_handoff":
                             self.nonsilent_inbound_frames_during_handoff}
                    if self._rep_first_audio_at is not None:
                        stats.update(
                            rep_first_audio_at=self._rep_first_audio_at,
                            rep_last_audio_at=self._rep_last_audio_at,
                            rep_first_line_end_at=self._rep_first_line_end_at,
                            nonsilent_inbound_frames_during_rep=len(self._nonsilent_inbound_timestamps_during_rep),
                            nonsilent_inbound_timestamps_during_rep=self._nonsilent_inbound_timestamps_during_rep)
                    with self._stats_path.open("a", encoding="utf-8") as out:
                        out.write(json.dumps(stats) + "\n")

    @property
    def nonsilent_inbound_frames_during_handoff(self) -> int:
        """AC-98 真机检查单用：转接期间收到的用户非静音音频帧数（只计数，不存音频）。"""
        return self._nonsilent_inbound_frames


class CarrierState:
    """号码 → 线路、线路 → 当前场景、call_sid → CarrierCall。"""

    def __init__(self, companies: list[CompanyConfig]) -> None:
        self.agent_mode = "sonic"
        self.company_dir: Path | None = None
        self.company_stats_path: Path | None = None
        self._settings: Any = None
        self._polly_client: Any = None
        self._prompt_caches: dict[tuple[str, str], PromptCache] = {}
        self._prompt_cache_lock = threading.Lock()
        self._companies = {c.line_number: c for c in companies}
        if len(self._companies) != len(companies):
            raise ScenarioError("Company line numbers must be unique")
        self._selections = {c.line_number: "baseline" for c in companies}
        self._calls: dict[str, CarrierCall] = {}
        self._tasks: dict[str, asyncio.Task[None]] = {}
        self._call_errors: dict[str, str] = {}

    def set_scenario(self, sel: ScenarioSelection) -> ScenarioSelection:
        """写入后下一通电话用它（R26、AC-84）。
        缺口 B-G-3（定稿 reject_4xx）：线路未知 → 404 {"code":"unknown_line"}；scenario_id 不在该线路所属公司的
        list_scenarios(company_dir, company_id) 里 → 404 {"code":"unknown_scenario"}；两种情况当前选择都不变。
        CarrierState 怎样拿到 company_dir 由 B 线自定（缺口 B-G-1，lane_internal）。"""
        company = self._company(sel.line_number, "unknown_line")
        root = self._root()
        if sel.scenario_id not in list_scenarios(root, company.company_id):
            raise HTTPException(status_code=404, detail=ApiError(
                code="unknown_scenario", message="The scenario is not available for this line.",
            ).model_dump())
        load_scenario(root, company.company_id, sel.scenario_id)
        self._selections[sel.line_number] = sel.scenario_id
        return sel

    def get_scenario(self, line_number: str) -> ScenarioSelection:
        self._company(line_number, "unknown_line")
        return ScenarioSelection(line_number=line_number, scenario_id=self._selections[line_number])

    async def place_call(self, dial: CarrierDialIn) -> CarrierDialOut:
        """号码未知 → 404 {"code":"unknown_number"}；否则建 CarrierCall 并在后台连接引擎，立即返回 queued。"""
        company = self._company(dial.to_number, "unknown_number")
        scenario_id = self._selections[dial.to_number]
        scenario = _live_scenario(load_scenario(self._root(), company.company_id, scenario_id))
        if self._settings is not None and self._settings.mode == "live-sim" and self.agent_mode == "scripted":
            check_scripted_steps(scenario, company)
        sid = new_call_sid()
        call = CarrierCall(dial=dial, call_sid=sid, scenario_id=scenario_id, company=company)
        call._agent_mode = self.agent_mode
        call._scenario = scenario
        call._stats_path = self.company_stats_path
        if self._settings is not None and self._settings.mode == "live-sim":
            rate = self._settings.engine.audio.wire_sample_rate
            if rate != MULAW_FRAME_BYTES * 1000 // LINE_FRAME_MS:
                raise ScenarioError("Configured wire sample rate differs from the line protocol")
            if not self._settings.aws_region:
                raise ScenarioError("AWS_REGION is required for Polly")
            call._prompt_cache = self._cache_for(company, company.voices.ivr_polly, rate)
            if self._settings.company_host and self._settings.company_port:
                host = self._settings.company_host
                netloc = f"[{host}]:{self._settings.company_port}" if ":" in host else f"{host}:{self._settings.company_port}"
                call._bot_endpoint = urlunsplit(("ws", netloc, "/ws/company-bot", "", ""))
            call._bot_threshold_dbfs = self._settings.engine.vad.threshold_dbfs
            call._sample_rate = rate
            if self.agent_mode == "scripted":
                call._engine_config = self._settings.engine
                call._voice_cache = lambda voice: self._cache_for(company, voice, rate)
        self._calls[sid] = call
        self._tasks[sid] = asyncio.create_task(self._run_call(sid, call))
        return CarrierDialOut(call_sid=sid, status="queued")

    def _cache_for(self, company: CompanyConfig, voice: str, rate: int) -> PromptCache:
        # Speech preparation runs in worker threads; publish one cache per voice.
        with self._prompt_cache_lock:
            key = (company.company_id, voice)
            if key not in self._prompt_caches:
                if self._polly_client is None:
                    import boto3.session
                    self._polly_client = boto3.session.Session(region_name=self._settings.aws_region).client("polly")
                self._prompt_caches[key] = PromptCache(self._polly_client, voice=voice, sample_rate=rate)
            return self._prompt_caches[key]

    async def hangup(self, call_sid: str) -> CarrierHangupOut:
        """call_sid 未知 → 404 {"code":"unknown_call"}（缺口 B-G-4，定稿 as_fake_carrier）。"""
        call = self._calls.get(call_sid)
        if call is None:
            raise HTTPException(status_code=404, detail=ApiError(
                code="unknown_call", message="The call was not found.",
            ).model_dump())
        await call.hangup()
        await self._cancel_call(call_sid)
        return CarrierHangupOut(call_sid=call_sid, status="completed")

    def _company(self, number: str, code: str) -> CompanyConfig:
        company = self._companies.get(number)
        if company is None:
            message = "The line was not found." if code == "unknown_line" else "The number was not found."
            raise HTTPException(status_code=404, detail=ApiError(code=code, message=message).model_dump())
        return company

    def _root(self) -> Path:
        if self.company_dir is None:
            raise ScenarioError("CarrierState.company_dir must be configured")
        return self.company_dir

    async def _run_call(self, sid: str, call: CarrierCall) -> None:
        try:
            await call.run()
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            # Exception text may contain the stream token. Keep only its type.
            self._call_errors[sid] = type(exc).__name__
            logging.getLogger(__name__).error("Carrier call %s failed (%s)", sid, type(exc).__name__)
        finally:
            await call.hangup()

    async def _cancel_call(self, sid: str) -> None:
        task = self._tasks.pop(sid, None)
        if task is not None:
            if not task.done():
                task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await task

    async def _shutdown(self) -> None:
        for sid in list(self._tasks):
            await self.hangup(sid)


def build_carrier_router(state: CarrierState) -> Any:
    """FastAPI APIRouter：POST /carrier/calls、POST /carrier/calls/{call_sid}/hangup、PUT/GET /sim/scenario、
    WS /ws/company-bot?role=agent|rep&call_sid=（运营商把媒体中继到公司方的 Sonic bot）。
    状态码与错误码见 models.CARRIER_ERROR_CODES 上方的说明（缺口 B-G-3、B-G-4）：成功一律 200，
    与共享的 tests/fakes/carrier.FakeCarrier 一致。"""
    router = APIRouter()

    async def respond(action: Any) -> Any:
        try:
            result = action()
            if asyncio.iscoroutine(result):
                result = await result
            return result
        except HTTPException as exc:
            return JSONResponse(status_code=exc.status_code, content=exc.detail)
        except ScenarioError:
            return JSONResponse(status_code=500, content=ApiError(
                code="invalid_configuration", message="The company configuration is invalid.",
            ).model_dump())

    @router.put("/sim/scenario", response_model=None)
    async def set_scenario(sel: ScenarioSelection) -> Any:
        return await respond(lambda: state.set_scenario(sel))

    @router.get("/sim/scenario", response_model=None)
    async def get_scenario(line_number: str) -> Any:
        return await respond(lambda: state.get_scenario(line_number))

    @router.post("/carrier/calls", response_model=None)
    async def place_call(dial: CarrierDialIn, request: Request) -> Any:
        result = await respond(lambda: state.place_call(dial))
        if isinstance(result, CarrierDialOut) and state._settings is not None and state._settings.mode == "live-sim":
            # Use the listening socket, including the OS-assigned port, never a caller-supplied Host.
            server = request.scope.get("server")
            if server is not None:
                host, port = server
                netloc = f"[{host}]:{port}" if ":" in host else f"{host}:{port}"
                state._calls[result.call_sid]._bot_endpoint = urlunsplit(("ws", netloc, "/ws/company-bot", "", ""))
        return result

    @router.post("/carrier/calls/{call_sid}/hangup", response_model=None)
    async def hangup(call_sid: str) -> Any:
        return await respond(lambda: state.hangup(call_sid))

    return router
