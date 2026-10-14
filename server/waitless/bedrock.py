"""Bedrock Converse 客户端（A 线；tech §3、§6.7、inv-1 §3；AC-22、AC-31；K13）。

公开接口（§7）：ConverseClient（协议，定义在 interfaces，这里再导出）、BotoConverseClient。
S0-r2 V9：Nova 2 Lite 目前被每日配额拦住（ThrottlingException）。所有 Lite 调用只经这里的接口；
默认测试一律用 tests/fakes 的 FakeConverse，不调用真实 Bedrock。

D14-1（用户决定、Jev 已裁）：同一个 ConverseClient 接口后面另有只供开发测试用的 CodexCliConverseClient，
经 Codex CLI 非交互调用；用可选变量 WAITLESS_PLANNER=bedrock|codex 切换，默认 bedrock（Nova 2 Lite）。
codex 只允许 text-sim（config.load_settings 在 live-sim、replay 下拒绝它）；最终演示、回放录制和视频都用 Nova 2 Lite。
命令与参数来自 WAITLESS_CODEX_COMMAND，代码里不写死命令、模型名或路径。llm_tasks.py 不变。
"""

import asyncio
import copy
import json
import time
import uuid
from botocore.exceptions import ClientError

from collections.abc import Awaitable, Callable, Sequence
from typing import Any

from waitless.config import Settings
from waitless.interfaces import ConverseClient
from waitless.models import ConverseCall

__all__ = [
    "BotoConverseClient",
    "CodexCliConverseClient",
    "CodexCliError",
    "ConverseClient",
    "ConverseThrottled",
    "build_converse_client",
    "build_forced_tool_config",
]


class ConverseThrottled(RuntimeError):
    """Bedrock 返回 ThrottlingException（如每日 token 配额用完）。不重试、不悄悄换模型或退到回放。"""


class BotoConverseClient:
    """boto3 bedrock-runtime 的 converse；同步调用放进线程执行。"""

    def __init__(self, *, region: str, client: Any | None = None) -> None:
        """client 为 None 时创建 boto3.client("bedrock-runtime", region_name=region)。
        代码里 boto3 客户端的服务名只能是 bedrock-runtime 和 polly（AC-31）。"""
        if client is None:
            import boto3
            from botocore.config import Config
            client = boto3.client("bedrock-runtime", region_name=region, config=Config(retries={"total_max_attempts": 1}))
        self.client = client

    async def converse(self, call: ConverseCall) -> dict[str, Any]:
        # todo(A): 参数映射见 models.ConverseCall 文档；asyncio.to_thread(client.converse, **kwargs)。
        # todo(A): ClientError 的 Error.Code == "ThrottlingException" → 抛 ConverseThrottled（K13）。
        kwargs = {"modelId": call.model_id, "system": call.system, "messages": call.messages}
        if call.tool_config is not None:
            kwargs["toolConfig"] = call.tool_config
        if call.inference_config is not None:
            kwargs["inferenceConfig"] = call.inference_config
        try:
            return await asyncio.to_thread(self.client.converse, **kwargs)
        except ClientError as exc:
            if exc.response.get("Error", {}).get("Code") == "ThrottlingException":
                raise ConverseThrottled("Planner quota exceeded") from None
            raise


class CodexCliError(RuntimeError):
    """Codex CLI 非零退出、超时，或输出对不上强制工具的 inputSchema。不重试、不退到 Lite 或回放（C14）。"""


# runner(argv, stdin_text, timeout_s) -> (exit_code, stdout, stderr)
CodexRunner = Callable[[Sequence[str], str, float], Awaitable[tuple[int, str, str]]]


class CodexCliConverseClient:
    """只供 text-sim 开发阶段用的 planner 备用实现（D14-1），实现 interfaces.ConverseClient。

    把 ConverseCall 的 system、messages 与（若有）强制工具的 inputSchema 拼成一段提示，经 stdin 交给
    `command`（来自 WAITLESS_CODEX_COMMAND，按 shlex 切分，例如一条 `codex exec ...` 命令）非交互运行，
    把输出转成与 boto3 converse 同形的响应：
      - 没有强制工具：{"output": {"message": {"role": "assistant", "content": [{"text": <输出>}]}}, "stopReason": "end_turn", ...}
      - 有强制工具（call.tool_config 的 toolChoice）：输出必须是满足该工具 inputSchema 的 JSON，转成
        content=[{"toolUse": {"toolUseId", "name", "input"}}]、stopReason="tool_use"；解析或校验失败抛 CodexCliError。
    runner 可注入：测试传假 runner，不起真实进程、不出网；None 时用 asyncio 子进程。
    """

    def __init__(self, *, command: Sequence[str], timeout_s: float, runner: CodexRunner | None = None) -> None:
        if not command or timeout_s <= 0:
            raise ValueError("command and positive timeout_s are required")
        self.command = tuple(command)
        self.timeout_s = timeout_s
        self.runner = runner or _run_codex

    async def converse(self, call: ConverseCall) -> dict[str, Any]:
        # todo(A): 按类说明实现；非零退出或超时抛 CodexCliError；usage 记 0，metrics.latencyMs 记实际耗时。
        prompt = json.dumps({"system": call.system, "messages": call.messages}, ensure_ascii=False)
        forced = (call.tool_config or {}).get("toolChoice", {}).get("tool", {}).get("name")
        spec = None
        if forced:
            spec = next((t["toolSpec"] for t in call.tool_config["tools"] if t["toolSpec"]["name"] == forced), None)
            if spec is None:
                raise CodexCliError("Forced tool has no specification")
            prompt += "\nReturn only a JSON object matching this extraction schema: " + json.dumps(spec["inputSchema"]["json"])
        else:
            prompt += "\nReturn only the requested reply text."
        start = time.monotonic()
        try:
            code, stdout, _stderr = await self.runner(self.command, prompt, self.timeout_s)
        except TimeoutError:
            raise CodexCliError("Planner command timed out") from None
        except OSError:
            raise CodexCliError("Planner command could not be started") from None
        if code != 0:
            raise CodexCliError(f"Planner command exited with status {code}")
        if forced:
            try:
                data = json.loads(stdout)
                import jsonschema
                jsonschema.validate(data, spec["inputSchema"]["json"])
            except (ValueError, jsonschema.ValidationError, jsonschema.SchemaError):
                raise CodexCliError("Planner output does not match the extraction schema") from None
            content = [{"toolUse": {"toolUseId": uuid.uuid4().hex, "name": forced, "input": data}}]
        else:
            content = [{"text": stdout.strip()}]
        return {"output": {"message": {"role": "assistant", "content": content}},
                "stopReason": "tool_use" if forced else "end_turn", "usage": {"inputTokens": 0, "outputTokens": 0, "totalTokens": 0},
                "metrics": {"latencyMs": int((time.monotonic()-start)*1000)}}


def build_converse_client(settings: Settings) -> ConverseClient:
    """按 settings.planner 选 planner 客户端（D14-1）：bedrock → BotoConverseClient(region=settings.aws_region)；
    codex → CodexCliConverseClient(command=settings.codex_command, timeout_s=settings.codex_timeout_s 或 A 定的默认)。
    replay 模式不经这里（用 ReplayPlayer 的 converse 通道）。"""
    if settings.planner == "codex":
        if settings.mode != "text-sim":
            raise ValueError("Codex planner requires text-sim")
        return CodexCliConverseClient(command=settings.codex_command or (), timeout_s=settings.codex_timeout_s or 60.0)
    if settings.mode == "replay" or not settings.aws_region:
        raise ValueError("Bedrock planner requires AWS_REGION outside replay")
    return BotoConverseClient(region=settings.aws_region)


def build_forced_tool_config(name: str, description: str, schema: dict[str, Any]) -> dict[str, Any]:
    """Converse 的 toolConfig：tools[0].toolSpec{name, description, inputSchema{json: schema}}，
    toolChoice={"tool": {"name": name}}（强制调用这个抽取工具；Nova 2 Lite 不支持结构化输出，事实 5）。"""
    schema = copy.deepcopy(schema)
    schema["required"] = list(schema.get("properties", {}))
    schema["additionalProperties"] = False
    return {"tools": [{"toolSpec": {"name": name, "description": description, "inputSchema": {"json": schema}}}],
            "toolChoice": {"tool": {"name": name}}}


async def _run_codex(argv, stdin_text, timeout_s):
    process = await asyncio.create_subprocess_exec(*argv, stdin=asyncio.subprocess.PIPE,
                stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE)
    try:
        stdout, stderr = await asyncio.wait_for(process.communicate(stdin_text.encode()), timeout_s)
    except BaseException:
        if process.returncode is None:
            process.kill()
        await process.wait()
        raise
    return process.returncode, stdout.decode(), stderr.decode()
