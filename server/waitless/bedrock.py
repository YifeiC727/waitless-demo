"""Bedrock Converse 客户端（A 线；tech §3、§6.7、inv-1 §3；AC-22、AC-31；K13）。

公开接口（§7）：ConverseClient（协议，定义在 interfaces，这里再导出）、BotoConverseClient。
S0-r2 V9：Nova 2 Lite 目前被每日配额拦住（ThrottlingException）。所有 Lite 调用只经这里的接口；
默认测试一律用 tests/fakes 的 FakeConverse，不调用真实 Bedrock。

D14-1（用户决定、Jev 已裁）：同一个 ConverseClient 接口后面另有只供开发测试用的 CodexCliConverseClient，
经 Codex CLI 非交互调用；用可选变量 WAITLESS_PLANNER=bedrock|codex 切换，默认 bedrock（Nova 2 Lite）。
codex 允许 text-sim 与 live-sim 开发测试，replay 禁止（E58 / D18）；最终演示、回放录制和视频都用 Nova 2 Lite。
命令与参数来自 WAITLESS_CODEX_COMMAND，代码里不写死命令、模型名或路径。llm_tasks.py 不变。

E82 / E83：anthropic 也只供 text-sim 与 live-sim 开发测试；用显式配置构造 AsyncAnthropic，
把 Converse 请求与 Messages 响应互译，只试一次；默认测试注入替身，不调用真实 API。
"""

import asyncio
import copy
import json
import math
import time
import uuid
import anthropic
import jsonschema
from botocore.exceptions import ClientError

from collections.abc import Awaitable, Callable, Sequence
from typing import Any

from waitless.config import Settings
from waitless.interfaces import ConverseClient
from waitless.models import ConverseCall

__all__ = [
    "ANTHROPIC_DEFAULT_MAX_TOKENS",
    "AnthropicConverseClient",
    "AnthropicPlannerError",
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


ANTHROPIC_DEFAULT_MAX_TOKENS = 4096


class AnthropicPlannerError(RuntimeError):
    """规划器错误；只保留分类元数据，避免泄露请求、响应或凭证。"""

    def __init__(self, kind: str, *, status_code: int | None = None, category: str | None = None) -> None:
        self.kind = kind
        self.status_code = status_code
        self.category = category
        super().__init__(f"kind={kind}, status_code={status_code}, category={category}")


class AnthropicConverseClient:
    """E83：Converse 与 Claude Messages 的适配；可注入假 SDK 客户端。"""

    def __init__(self, *, api_key: str | None = None, base_url: str | None = None,
                 timeout_s: float | None = None, client: Any | None = None) -> None:
        if client is None:
            if (not api_key or not api_key.strip() or not base_url or not base_url.strip()
                    or timeout_s is None or not math.isfinite(timeout_s) or timeout_s <= 0):
                raise ValueError("api_key, base_url and positive timeout_s are required")
            client = anthropic.AsyncAnthropic(api_key=api_key, base_url=base_url, timeout=timeout_s, max_retries=0)
        self.client = client

    @staticmethod
    def _text_blocks(blocks: list[dict[str, Any]]) -> list[dict[str, str]]:
        translated = []
        for block in blocks:
            if set(block) != {"text"} or not isinstance(block["text"], str):
                raise AnthropicPlannerError("unsupported") from None
            translated.append({"type": "text", "text": block["text"]})
        return translated

    @classmethod
    def _request_kwargs(cls, call: ConverseCall) -> dict[str, Any]:
        """不支持的输入在 SDK 调用前拒绝；采样参数不进入请求。"""
        try:
            if call.messages[-1]["role"] == "assistant":
                raise AnthropicPlannerError("unsupported") from None
            inference = call.inference_config or {}
            kwargs = {
                "model": call.model_id,
                "messages": [{"role": message["role"], "content": cls._text_blocks(message["content"])}
                             for message in call.messages],
                "max_tokens": inference.get("maxTokens", ANTHROPIC_DEFAULT_MAX_TOKENS),
                "output_config": {"effort": "low"},
            }
            if call.system:
                kwargs["system"] = cls._text_blocks(call.system)
            if "stopSequences" in inference:
                kwargs["stop_sequences"] = inference["stopSequences"]
            if call.tool_config is not None:
                kwargs["tools"] = [
                    {"name": spec["name"], "description": spec["description"],
                     "input_schema": spec["inputSchema"]["json"]}
                    for tool in call.tool_config["tools"] for spec in [tool["toolSpec"]]
                ]
                if "toolChoice" in call.tool_config:
                    choice = call.tool_config["toolChoice"]
                    if set(choice) == {"tool"}:
                        kwargs["tool_choice"] = {"type": "tool", "name": choice["tool"]["name"],
                                                 "disable_parallel_tool_use": True}
                    elif set(choice) == {"any"}:
                        kwargs["tool_choice"] = {"type": "any", "disable_parallel_tool_use": True}
                    elif set(choice) == {"auto"}:
                        kwargs["tool_choice"] = {"type": "auto"}
                    else:
                        raise AnthropicPlannerError("unsupported") from None
            return kwargs
        except (KeyError, TypeError, AttributeError, IndexError):
            raise AnthropicPlannerError("unsupported") from None

    async def converse(self, call: ConverseCall) -> dict[str, Any]:
        kwargs = self._request_kwargs(call)
        start = time.monotonic()
        try:
            response = await self.client.messages.create(**kwargs)
        except anthropic.RateLimitError:
            raise ConverseThrottled("Planner quota exceeded") from None
        except anthropic.APITimeoutError:
            raise AnthropicPlannerError("timeout") from None
        except anthropic.APIConnectionError:
            raise AnthropicPlannerError("connection") from None
        except anthropic.APIStatusError as exc:
            raise AnthropicPlannerError("api_status", status_code=exc.status_code) from None
        except anthropic.AnthropicError:
            raise AnthropicPlannerError("api_status") from None

        stop_reason = response.stop_reason
        if stop_reason == "refusal":
            details = getattr(response, "stop_details", None)
            category = details.get("category") if isinstance(details, dict) else getattr(details, "category", None)
            raise AnthropicPlannerError("refusal", category=category) from None
        if stop_reason in ("max_tokens", "model_context_window_exceeded"):
            raise AnthropicPlannerError("truncated") from None
        if stop_reason not in ("end_turn", "tool_use", "stop_sequence"):
            raise AnthropicPlannerError("unexpected_stop") from None
        content = []
        for block in response.content:
            if block.type == "text":
                content.append({"text": block.text})
            elif block.type == "tool_use":
                content.append({"toolUse": {"toolUseId": block.id, "name": block.name, "input": block.input}})
        if not content:
            raise AnthropicPlannerError("empty") from None
        input_tokens = response.usage.input_tokens
        output_tokens = response.usage.output_tokens
        return {
            "output": {"message": {"role": "assistant", "content": content}},
            "stopReason": stop_reason,
            "usage": {"inputTokens": input_tokens, "outputTokens": output_tokens,
                      "totalTokens": input_tokens + output_tokens},
            "metrics": {"latencyMs": int((time.monotonic() - start) * 1000)},
        }


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
    """只供 text-sim 与 live-sim 开发测试用的 planner 备用实现（E58 / D18），实现 interfaces.ConverseClient。

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
        except UnicodeError:
            raise CodexCliError("Planner command output could not be decoded") from None
        if code != 0:
            raise CodexCliError(f"Planner command exited with status {code}")
        if forced:
            try:
                data = json.loads(stdout)
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
    """按 settings.planner 选 Bedrock、Codex CLI 或 Anthropic 客户端（E58 / D18 / E82）。
    anthropic 显式传入 Settings 中的凭证、根地址与超时；codex 使用配置的命令与超时。
    codex 与 anthropic 只供开发测试，text-sim 与 live-sim 可用，replay 禁止。
    replay 模式不经这里（用 ReplayPlayer 的 converse 通道）。"""
    if settings.planner == "codex":
        if settings.mode == "replay":
            raise ValueError("Codex planner is not allowed in replay mode")
        return CodexCliConverseClient(command=settings.codex_command or (), timeout_s=settings.codex_timeout_s or 60.0)
    if settings.planner == "anthropic":
        if settings.mode == "replay":
            raise ValueError("Anthropic planner is not allowed in replay mode")
        return AnthropicConverseClient(
            api_key=settings.anthropic_api_key.get_secret_value() if settings.anthropic_api_key is not None else None,
            base_url=settings.anthropic_base_url,
            timeout_s=settings.anthropic_timeout_s,
        )
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
