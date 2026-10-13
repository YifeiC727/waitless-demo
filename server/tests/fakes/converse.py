"""FakeConverse：按任务脚本应答的 Bedrock Converse 客户端（tech §12；实现 waitless.interfaces.ConverseClient）。

S0-r2 V9：Nova 2 Lite 被每日配额拦住，所有 Lite 调用只经 bedrock.py 的 ConverseClient，
默认测试一律用本替身。返回值是 boto3 bedrock-runtime converse 的响应形状：

    {"output": {"message": {"role": "assistant", "content": [...]}},
     "stopReason": "end_turn" | "tool_use", "usage": {...}, "metrics": {"latencyMs": 0}}

脚本条目：响应字典（用 converse_text()/converse_tool_use() 生成）、可调用对象 f(call) → 响应字典、
或异常实例（如 FakeThrottlingError()，模拟 ThrottlingException）。
"""

from collections import deque
from collections.abc import Callable, Iterable, Mapping
from typing import Any

from waitless.models import ConverseCall

Entry = Any


def converse_text(text: str, *, stop_reason: str = "end_turn") -> dict[str, Any]:
    return {
        "output": {"message": {"role": "assistant", "content": [{"text": text}]}},
        "stopReason": stop_reason,
        "usage": {"inputTokens": 0, "outputTokens": 0, "totalTokens": 0},
        "metrics": {"latencyMs": 0},
    }


def converse_tool_use(name: str, tool_input: Mapping[str, Any], *, tool_use_id: str = "tooluse-fake-1") -> dict[str, Any]:
    return {
        "output": {
            "message": {
                "role": "assistant",
                "content": [{"toolUse": {"toolUseId": tool_use_id, "name": name, "input": dict(tool_input)}}],
            }
        },
        "stopReason": "tool_use",
        "usage": {"inputTokens": 0, "outputTokens": 0, "totalTokens": 0},
        "metrics": {"latencyMs": 0},
    }


class FakeThrottlingError(Exception):
    """形状同 botocore ClientError 的 ThrottlingException（不导入 botocore）。"""

    def __init__(self, message: str = "Too many tokens per day, please wait before trying again.") -> None:
        super().__init__(message)
        self.response = {"Error": {"Code": "ThrottlingException", "Message": message}}
        self.operation_name = "Converse"


def forced_tool_name(call: ConverseCall) -> str | None:
    """call.tool_config 里 toolChoice 强制的工具名；没有强制时为 None。"""
    choice = (call.tool_config or {}).get("toolChoice") or {}
    tool = choice.get("tool") if isinstance(choice, Mapping) else None
    return tool.get("name") if isinstance(tool, Mapping) else None


class FakeConverse:
    def __init__(
        self,
        responses: Mapping[str, Iterable[Entry]] | None = None,
        *,
        default: Callable[[ConverseCall], dict[str, Any]] | None = None,
    ) -> None:
        self._queues: dict[str, deque[Entry]] = {k: deque(v) for k, v in (responses or {}).items()}
        self.default = default
        self.calls: list[ConverseCall] = []

    def add(self, task: str, *entries: Entry) -> "FakeConverse":
        self._queues.setdefault(task, deque()).extend(entries)
        return self

    def calls_for(self, task: str) -> list[ConverseCall]:
        return [c for c in self.calls if c.task == task]

    def remaining(self, task: str | None = None) -> int:
        if task is not None:
            return len(self._queues.get(task, ()))
        return sum(len(q) for q in self._queues.values())

    async def converse(self, call: ConverseCall | Mapping[str, Any]) -> dict[str, Any]:
        c = call if isinstance(call, ConverseCall) else ConverseCall.model_validate(call)
        self.calls.append(c)
        q = self._queues.get(c.task)
        if q:
            entry = q.popleft()
        elif self.default is not None:
            entry = self.default
        else:
            raise AssertionError(f"FakeConverse: no scripted response for task {c.task!r}")
        if isinstance(entry, BaseException):
            raise entry
        if callable(entry):
            entry = entry(c)
        if not isinstance(entry, Mapping) or "output" not in entry:
            raise AssertionError("FakeConverse entry must be a Converse response dict")
        return dict(entry)


__all__ = [
    "FakeConverse",
    "FakeThrottlingError",
    "converse_text",
    "converse_tool_use",
    "forced_tool_name",
]
