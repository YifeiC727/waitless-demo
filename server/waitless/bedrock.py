"""Bedrock Converse 客户端（A 线；tech §3、§6.7、inv-1 §3；AC-22、AC-31；K13）。

公开接口（§7）：ConverseClient（协议，定义在 interfaces，这里再导出）、BotoConverseClient。
S0-r2 V9：Nova 2 Lite 目前被每日配额拦住（ThrottlingException）。所有 Lite 调用只经这里的接口；
默认测试一律用 tests/fakes 的 FakeConverse，不调用真实 Bedrock。

D14-1（用户决定、Jev 已裁）：同一个 ConverseClient 接口后面另有只供开发测试用的 CodexCliConverseClient，
经 Codex CLI 非交互调用；用可选变量 WAITLESS_PLANNER=bedrock|codex 切换，默认 bedrock（Nova 2 Lite）。
codex 只允许 text-sim（config.load_settings 在 live-sim、replay 下拒绝它）；最终演示、回放录制和视频都用 Nova 2 Lite。
命令与参数来自 WAITLESS_CODEX_COMMAND，代码里不写死命令、模型名或路径。llm_tasks.py 不变。
"""

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
        # todo(A): 关闭 SDK 自动重试（botocore Config(retries={"total_max_attempts": 1})，S0-r2 V9 的做法）。
        raise NotImplementedError

    async def converse(self, call: ConverseCall) -> dict[str, Any]:
        # todo(A): 参数映射见 models.ConverseCall 文档；asyncio.to_thread(client.converse, **kwargs)。
        # todo(A): ClientError 的 Error.Code == "ThrottlingException" → 抛 ConverseThrottled（K13）。
        raise NotImplementedError


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
        # todo(A): command 为空抛 ValueError；不在日志里打印提示全文（可能含档案的可代说字段）。
        raise NotImplementedError

    async def converse(self, call: ConverseCall) -> dict[str, Any]:
        # todo(A): 按类说明实现；非零退出或超时抛 CodexCliError；usage 记 0，metrics.latencyMs 记实际耗时。
        raise NotImplementedError


def build_converse_client(settings: Settings) -> ConverseClient:
    """按 settings.planner 选 planner 客户端（D14-1）：bedrock → BotoConverseClient(region=settings.aws_region)；
    codex → CodexCliConverseClient(command=settings.codex_command, timeout_s=settings.codex_timeout_s 或 A 定的默认)。
    replay 模式不经这里（用 ReplayPlayer 的 converse 通道）。"""
    raise NotImplementedError


def build_forced_tool_config(name: str, description: str, schema: dict[str, Any]) -> dict[str, Any]:
    """Converse 的 toolConfig：tools[0].toolSpec{name, description, inputSchema{json: schema}}，
    toolChoice={"tool": {"name": name}}（强制调用这个抽取工具；Nova 2 Lite 不支持结构化输出，事实 5）。"""
    # todo(A): inv-1 §3 的格式；schema 的 required 列全部字段、additionalProperties=false（S0-r2 V9）。
    raise NotImplementedError
