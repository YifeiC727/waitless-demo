"""按通道回放（E 线；tech §6.12；AC-88、AC-89；K16）。

公开接口（§7）：ReplayPlayer.next(channel, shape)、ReplayMismatch（定义在 interfaces，这里再导出）。
回放对不上就报错，绝不改走在线调用（Q6、C14）。
"""

from collections.abc import Mapping
from copy import deepcopy
from pathlib import Path
import re
from typing import Any

from pydantic import ValidationError

from waitless.decision.registry import Registry
from waitless.interfaces import ConverseClient, JevClient, ReplayMismatch
from waitless.models import ConverseCall, JevQuestion, JevResponse, JevState, ReplayHeader, ReplayRecord, ReplayShape

__all__ = ["ReplayMismatch", "ReplayPlayer"]


class ReplayPlayer:
    def __init__(self, path: Path, *, registry: Registry, substitutions: Mapping[str, str] | None = None) -> None:
        """读 replays/<name>.jsonl：第一行 ReplayHeader，其余 ReplayRecord。
        registry：从题名反推 point 来核对 shape（A-06）。substitutions：占位符 → 当前值（{{task_id}}、{{call_id}}）。"""
        self._registry = registry
        self._substitutions = dict(substitutions or {})
        self._first_mismatch: ReplayMismatch | None = None
        self._records: list[ReplayRecord] = []
        self._channels: dict[str, list[ReplayRecord]] = {}
        self._used: dict[str, int] = {}
        try:
            lines = Path(path).read_text(encoding="utf-8").splitlines()
        except (OSError, UnicodeError):
            raise self._mismatch("file", 0, str(path)) from None
        nonempty = [(index, line) for index, line in enumerate(lines) if line.strip()]
        if not nonempty:
            raise self._mismatch("file", 0)
        header_index, header_line = nonempty[0]
        try:
            self._header = ReplayHeader.model_validate_json(header_line)
        except ValidationError:
            raise self._mismatch("file", 0) from None
        for index, line in nonempty[1:]:
            try:
                record = ReplayRecord.model_validate_json(line)
            except ValidationError:
                raise self._mismatch("file", index - header_index) from None
            self._records.append(record)
            self._channels.setdefault(record.channel, []).append(record)

    @property
    def header(self) -> ReplayHeader:
        """header.scenario_id 是录制时的场景；replay 模式强制用它（A-11）。header.request_text 由 A 的 /app-config
        作为 replay_request_text 返回（X-G3）。用哪个文件由 A 按 WAITLESS_REPLAY_DIR 与 WAITLESS_REPLAY_FILE 定（X-G5）。"""
        return self._header

    def next(self, channel: str, shape: ReplayShape) -> dict[str, Any]:
        """按通道 FIFO 取下一条；缺行或 shape 不一致抛 ReplayMismatch(channel, seq)（AC-89）。"""
        records = self._channels.get(channel, [])
        used = self._used.get(channel, 0)
        if used == len(records):
            raise self._mismatch(channel, used)
        record = records[used]
        expected = record.shape
        if (expected.point != shape.point or expected.task != shape.task
                or expected.tool != shape.tool
                or set(expected.question_ids or []) != set(shape.question_ids or [])):
            raise self._mismatch(channel, record.seq)
        self._used[channel] = used + 1
        return _replace_strings(record.response, self._substitutions)

    def bind(self, *, task_id: str | None = None, call_id: str | None = None) -> None:
        """设置占位符替换的当前值。"""
        if task_id is not None:
            self._substitutions["{{task_id}}"] = task_id
        if call_id is not None:
            self._substitutions["{{call_id}}"] = call_id

    def jev_client(self) -> JevClient:
        """实现 JevClient：ask(state, questions) → next("jev", {point: registry.point_of(questions 的键), question_ids})（A-06）。"""
        return _ReplayJevClient(self)

    def converse_client(self) -> ConverseClient:
        """实现 ConverseClient：converse(call) → next(f"converse:{call.task}", {task, tool})。"""
        return _ReplayConverseClient(self)

    def remaining(self, channel: str | None = None) -> int:
        if channel is not None:
            return len(self._channels.get(channel, [])) - self._used.get(channel, 0)
        return len(self._records) - sum(self._used.values())

    def _mismatch(self, channel: str, seq: int, detail: str = "") -> ReplayMismatch:
        mismatch = ReplayMismatch(channel, seq, detail)
        if self._first_mismatch is None:
            self._first_mismatch = mismatch
        return mismatch

    def _next_seq(self, channel: str) -> int:
        used = self._used.get(channel, 0)
        records = self._channels.get(channel, [])
        return records[used].seq if used < len(records) else used

    def _first_remaining(self) -> tuple[str, int] | None:
        positions: dict[str, int] = {}
        for record in self._records:
            position = positions.get(record.channel, 0)
            if position >= self._used.get(record.channel, 0):
                return record.channel, record.seq
            positions[record.channel] = position + 1
        return None


class _ReplayJevClient:
    def __init__(self, player: ReplayPlayer) -> None:
        self._player = player

    async def ask(self, state: JevState, questions: Mapping[str, JevQuestion]) -> JevResponse:
        seq = self._player._next_seq("jev")
        try:
            point = self._player._registry.point_of(questions.keys())
        except Exception:
            raise self._player._mismatch("jev", seq) from None
        shape = ReplayShape(point=point, question_ids=list(questions))
        response = self._player.next("jev", shape)
        try:
            return JevResponse.model_validate(response)
        except ValidationError:
            raise self._player._mismatch("jev", seq) from None


class _ReplayConverseClient:
    def __init__(self, player: ReplayPlayer) -> None:
        self._player = player

    async def converse(self, call: ConverseCall) -> dict[str, Any]:
        tool = _forced_tool(call)
        return self._player.next(f"converse:{call.task}", ReplayShape(task=call.task, tool=tool))


def _forced_tool(call: ConverseCall) -> str | None:
    value = call.tool_config
    for key in ("toolChoice", "tool", "name"):
        if not isinstance(value, Mapping):
            return None
        value = value.get(key)
    return value


def _replace_strings(value: Any, replacements: Mapping[str, str]) -> Any:
    """Copy JSON data and substitute once, so inserted values are never rewritten."""
    if isinstance(value, str):
        keys = sorted((key for key in replacements if key), key=len, reverse=True)
        if not keys:
            return value
        return re.sub("|".join(re.escape(key) for key in keys),
                      lambda match: replacements[match.group()], value)
    if isinstance(value, Mapping):
        return {_replace_strings(key, replacements): _replace_strings(item, replacements)
                for key, item in value.items()}
    if isinstance(value, list):
        return [_replace_strings(item, replacements) for item in value]
    return deepcopy(value)
