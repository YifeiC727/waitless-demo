"""录制回放（E 线；tech §6.12；AC-88）。text-sim 下包住真实的 Jev 与 Converse 客户端，逐条写 replays/<name>.jsonl。

公开接口（§7）：ReplayRecorder。只录 JSON，不录音频（C7）。
"""

from collections.abc import Mapping
from pathlib import Path
from typing import Any

from waitless.decision.registry import Registry
from waitless.interfaces import ConverseClient, JevClient
from waitless.models import ConverseCall, JevQuestion, JevResponse, JevState, ReplayHeader, ReplayRecord, ReplayShape
from waitless.replay.player import _forced_tool, _replace_strings

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
        self._registry = registry
        self._placeholders = dict(placeholders or {})
        self._seq: dict[str, int] = {}
        path = Path(path)
        path.parent.mkdir(parents=True, exist_ok=True)
        self._file = path.open("x", encoding="utf-8")
        try:
            self._file.write(header.model_dump_json() + "\n")
            self._file.flush()
        except Exception:
            self._file.close()
            raise

    def wrap_jev(self, client: JevClient) -> JevClient:
        """返回同接口的客户端 ask(state, questions)：转发请求并录
        {"channel":"jev","seq","shape":{point: registry.point_of(questions 的键), question_ids},"response"}（A-06）。"""
        return _RecordingJevClient(self, client)

    def wrap_converse(self, client: ConverseClient) -> ConverseClient:
        """录 {"channel":"converse:<task>","shape":{task, tool}}；tool 是强制工具名或 null。"""
        return _RecordingConverseClient(self, client)

    def record(self, channel: str, shape: ReplayShape, response: Mapping[str, Any]) -> None:
        """task_id、call_id 替换成占位符后追加一行；同一通道 seq 递增。"""
        seq = self._seq.get(channel, 0)
        record = ReplayRecord(channel=channel, seq=seq, shape=shape,
                              response=_replace_strings(response, self._placeholders))
        self._file.write(record.model_dump_json(exclude_none=True) + "\n")
        self._file.flush()
        self._seq[channel] = seq + 1

    def bind(self, *, task_id: str | None = None, call_id: str | None = None) -> None:
        """缺口 E-G-8（定稿 recorder_bind）：设置要替换成占位符的当前 id（与 ReplayPlayer.bind 对称）；
        拿到 call_id 后由调用方（run_eval 或 A 的 CallManager.start）调用，之后录的行里这些 id 换成 {{task_id}}、{{call_id}}。"""
        for actual, placeholder in ((task_id, "{{task_id}}"), (call_id, "{{call_id}}")):
            if actual is not None:
                self._placeholders = {key: value for key, value in self._placeholders.items()
                                      if value != placeholder}
                self._placeholders[actual] = placeholder

    def close(self) -> None:
        self._file.close()


class _RecordingJevClient:
    def __init__(self, recorder: ReplayRecorder, client: JevClient) -> None:
        self._recorder, self._client = recorder, client

    async def ask(self, state: JevState, questions: Mapping[str, JevQuestion]) -> JevResponse:
        response = await self._client.ask(state, questions)
        shape = ReplayShape(point=self._recorder._registry.point_of(questions.keys()),
                            question_ids=list(questions))
        self._recorder.record("jev", shape, response.model_dump(mode="json", exclude_none=True))
        return response


class _RecordingConverseClient:
    def __init__(self, recorder: ReplayRecorder, client: ConverseClient) -> None:
        self._recorder, self._client = recorder, client

    async def converse(self, call: ConverseCall) -> dict[str, Any]:
        response = await self._client.converse(call)
        self._recorder.record(f"converse:{call.task}",
                              ReplayShape(task=call.task, tool=_forced_tool(call)), response)
        return response
