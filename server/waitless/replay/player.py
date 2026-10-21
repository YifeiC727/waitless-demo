"""按通道回放（E 线；tech §6.12；AC-88、AC-89；K16）。

公开接口（§7）：ReplayPlayer.next(channel, shape)、ReplayMismatch（定义在 interfaces，这里再导出）。
回放对不上就报错，绝不改走在线调用（Q6、C14）。
"""

from collections.abc import Mapping
from pathlib import Path
from typing import Any

from waitless.decision.registry import Registry
from waitless.interfaces import ConverseClient, JevClient, ReplayMismatch
from waitless.models import ReplayHeader, ReplayShape

__all__ = ["ReplayMismatch", "ReplayPlayer"]


class ReplayPlayer:
    def __init__(self, path: Path, *, registry: Registry, substitutions: Mapping[str, str] | None = None) -> None:
        """读 replays/<name>.jsonl：第一行 ReplayHeader，其余 ReplayRecord。
        registry：从题名反推 point 来核对 shape（A-06）。substitutions：占位符 → 当前值（{{task_id}}、{{call_id}}）。"""
        raise NotImplementedError

    @property
    def header(self) -> ReplayHeader:
        """header.scenario_id 是录制时的场景；replay 模式强制用它（A-11）。header.request_text 由 A 的 /app-config
        作为 replay_request_text 返回（X-G3）。用哪个文件由 A 按 WAITLESS_REPLAY_DIR 与 WAITLESS_REPLAY_FILE 定（X-G5）。"""
        raise NotImplementedError

    def next(self, channel: str, shape: ReplayShape) -> dict[str, Any]:
        """按通道 FIFO 取下一条；缺行或 shape 不一致抛 ReplayMismatch(channel, seq)（AC-89）。"""
        raise NotImplementedError

    def bind(self, *, task_id: str | None = None, call_id: str | None = None) -> None:
        """设置占位符替换的当前值。"""
        raise NotImplementedError

    def jev_client(self) -> JevClient:
        """实现 JevClient：ask(state, questions) → next("jev", {point: registry.point_of(questions 的键), question_ids})（A-06）。"""
        raise NotImplementedError

    def converse_client(self) -> ConverseClient:
        """实现 ConverseClient：converse(call) → next(f"converse:{call.task}", {task, tool})。"""
        raise NotImplementedError

    def remaining(self, channel: str | None = None) -> int:
        raise NotImplementedError
