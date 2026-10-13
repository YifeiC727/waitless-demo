"""录制回放（E 线；tech §6.12；AC-88）。text-sim 下包住真实的 Jev 与 Converse 客户端，逐条写 replays/<name>.jsonl。

公开接口（§7）：ReplayRecorder。只录 JSON，不录音频（C7）。
"""

from collections.abc import Mapping
from pathlib import Path
from typing import Any

from waitless.decision.registry import Registry
from waitless.interfaces import ConverseClient, JevClient
from waitless.models import ReplayHeader, ReplayShape

__all__ = ["ReplayRecorder"]


class ReplayRecorder:
    def __init__(
        self,
        path: Path,
        header: ReplayHeader,
        *,
        registry: Registry,
        placeholders: Mapping[str, str] | None = None,
    ) -> None:
        """placeholders：实际 id → 占位符，如 {"<task_id>": "{{task_id}}", "<call_id>": "{{call_id}}"}。
        registry：反推 jev 通道的 shape.point（A-06）。header.scenario_id 记录录制时的场景（A-11），
        header.request_text 记录用户原话（X-G3），header.models.planner 写 "<provider>:<model>"（D14-1）。
        输出文件名约定 <scenario_id>.jsonl，已存在就报错、不覆盖（X-G4，由创建 path 的调用方保证）。"""
        raise NotImplementedError

    def wrap_jev(self, client: JevClient) -> JevClient:
        """返回同接口的客户端 ask(state, questions)：转发请求并录
        {"channel":"jev","seq","shape":{point: registry.point_of(questions 的键), question_ids},"response"}（A-06）。"""
        raise NotImplementedError

    def wrap_converse(self, client: ConverseClient) -> ConverseClient:
        """录 {"channel":"converse:<task>","shape":{task, tool}}；tool 是强制工具名或 null。"""
        raise NotImplementedError

    def record(self, channel: str, shape: ReplayShape, response: Mapping[str, Any]) -> None:
        """task_id、call_id 替换成占位符后追加一行；同一通道 seq 递增。"""
        raise NotImplementedError

    def bind(self, *, task_id: str | None = None, call_id: str | None = None) -> None:
        """缺口 E-G-8（定稿 recorder_bind）：设置要替换成占位符的当前 id（与 ReplayPlayer.bind 对称）；
        拿到 call_id 后由调用方（run_eval 或 A 的 CallManager.start）调用，之后录的行里这些 id 换成 {{task_id}}、{{call_id}}。"""
        raise NotImplementedError

    def close(self) -> None:
        raise NotImplementedError
