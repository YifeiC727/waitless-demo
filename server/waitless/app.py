"""组装 FastAPI 应用（A 线；tech §6.2、§6.3、§6.4、inv-1 §2.1；AC-01、AC-18）。

公开接口（§7）：create_app(settings)。路由注册顺序有要求：先注册所有 FastAPI 路由与静态文件，
最后把 MCP 的 http_app 挂到 "/"（内部路径 /mcp）；MCP 的 session_manager.run() 在父 lifespan 里跑。

缺口 A-G-1（定稿 app_parts_dataclass）：A 线自有的 AppParts 装 MCP 工具与 /assistant/turn 要的 A 线服务；
冻结的 interfaces.AppServices 不动（D 的端点只读它）。create_app 与 ToolHandlers 收同一个 AppParts。
缺口 E-G-3（定稿 build_services_injection）：build_services 可注入 Jev、Converse 客户端与时钟，E、X 的整条链
测试经它组装，不碰 A 的内部方法。
"""

from dataclasses import dataclass
from typing import TYPE_CHECKING

from waitless.assistant import AssistantService
from waitless.calls import CallManager
from waitless.config import Settings
from waitless.interfaces import AppServices, Clock, ConverseClient, JevClient
from waitless.models import TrustedSources
from waitless.official_number import Fetcher
from waitless.results import ResultService
from waitless.tasks import TaskService

if TYPE_CHECKING:
    from fastapi import FastAPI

__all__ = ["AppParts", "build_services", "create_app"]


@dataclass
class AppParts:
    """A 线内部服务（缺口 A-G-1）。ToolHandlers(services, parts) 与 create_app(settings, parts=...) 都收它。

    tasks：prepare_task、update_task；calls：开始通话与通话中的工具（也是 AppServices.calls）；
    results：get_case_result 与挂断后的结果流程；assistant：POST /assistant/turn；
    fetcher、trusted_sources：find_official_number（trusted_sources 是 find-official-number skill 的
    references/trusted-sources.yaml，按 models.TrustedSources 校验，A-G-4）。
    """

    tasks: TaskService
    calls: CallManager
    results: ResultService
    assistant: AssistantService
    fetcher: Fetcher
    trusted_sources: TrustedSources


def create_app(
    settings: Settings,
    *,
    services: AppServices | None = None,
    parts: AppParts | None = None,
) -> "FastAPI":
    """返回完整的 FastAPI 应用。services、parts 都为 None 时用 build_services(settings) 组装；
    只给其中一个时，另一个也由 build_services 组装（测试可以只替换其中一份）。"""
    import json
    from contextlib import asynccontextmanager
    from fastapi import FastAPI, WebSocket
    from fastapi.responses import FileResponse
    from fastapi.staticfiles import StaticFiles
    from mcp.server.transport_security import TransportSecuritySettings
    from urllib.parse import urlunsplit
    from waitless import __version__
    from waitless.mcp_server import build_mcp, ToolHandlers
    from waitless.models import AppConfigOut, AssistantTurnIn, AssistantTurnOut, HealthOut, ReplayHeader
    if services is None or parts is None:
        built_services, built_parts = build_services(settings)
        services = services or built_services
        parts = parts or built_parts
    mcp = build_mcp(services, ToolHandlers(services, parts))
    hosts = list(settings.allowed_hosts)
    origins = [urlunsplit((scheme, host, "", "", "")) for host in hosts for scheme in ("http", "https")]
    http_app = mcp.streamable_http_app(stateless_http=True, json_response=True, streamable_http_path="/mcp",
                 host=settings.host, transport_security=TransportSecuritySettings(
                     enable_dns_rebinding_protection=True, allowed_hosts=hosts, allowed_origins=origins))
    @asynccontextmanager
    async def lifespan(app):
        async with http_app.router.lifespan_context(http_app):
            try:
                yield
            finally:
                await parts.calls.shutdown()
    app = FastAPI(lifespan=lifespan)
    app.state.services = services
    app.state.parts = parts
    @app.get("/", include_in_schema=False)
    async def index():
        return FileResponse(settings.web_dir / "index.html", media_type="text/html")
    app.mount("/js", StaticFiles(directory=settings.web_dir / "js", check_dir=False), name="js")
    @app.get("/app-config", response_model=AppConfigOut)
    async def app_config():
        text = None
        if settings.mode == "replay":
            path = settings.replay_dir / (settings.replay_file or "baseline-web.jsonl")
            with path.open(encoding="utf-8") as source:
                text = ReplayHeader.model_validate_json(source.readline()).request_text
        return AppConfigOut(mode=settings.mode, poll_ms=settings.engine.web.poll_ms, replay_request_text=text)
    @app.get("/healthz", response_model=HealthOut)
    async def health():
        return HealthOut(ok=True, mode=settings.mode, version=__version__)
    @app.post("/assistant/turn", response_model=AssistantTurnOut)
    async def assistant_turn(inp: AssistantTurnIn):
        return await parts.assistant.turn(inp)
    if settings.mode == "live-sim":
        @app.websocket("/ws/line")
        async def line(websocket: WebSocket):
            from waitless.call.line_ws import line_endpoint
            await line_endpoint(websocket)
    @app.websocket("/ws/handoff/{handoff_id}")
    async def handoff(websocket: WebSocket, handoff_id: str):
        from waitless.call.handoff import handoff_endpoint
        await handoff_endpoint(websocket, handoff_id)
    app.mount("/", http_app)
    return app


def build_services(
    settings: Settings,
    *,
    jev: JevClient | None = None,
    converse: ConverseClient | None = None,
    clock: Clock | None = None,
) -> tuple[AppServices, AppParts]:
    """按模式组装 Store、Trace、Clock、决策门、CallManager、令牌签发器等，返回 (AppServices, AppParts)。

    jev、converse、clock 给出时代替在线客户端与系统时钟（缺口 E-G-3）；cli 与 create_app 不传。
    """
    # todo(A): replay 模式下 Jev 与 Converse 客户端都换成 replay.player.ReplayPlayer 的通道（AC-88）；
    #          回放文件 = WAITLESS_REPLAY_DIR / (WAITLESS_REPLAY_FILE 或默认的 baseline-web.jsonl)（缺口 X-G5）；
    #          ReplayPlayer 要拿到 registry（A-06）；CallManager 的 replay_scenario_id = player.header.scenario_id（A-11）。
    # todo(A): live-sim / text-sim 用 decision.jev.HttpJevClient；planner 客户端用 bedrock.build_converse_client(settings)
    #          （默认 BotoConverseClient；WAITLESS_PLANNER=codex 时是只供 text-sim 开发用的 CodexCliConverseClient，D14-1）。
    # todo(A): text-sim 设了 WAITLESS_RECORD_DIR 时用 ReplayRecorder 包住两个客户端；头里 models.planner 按
    #          "<provider>:<model>" 如实写（D14-1），request_text 写 prepare_task 的用户原话（X-G3）；
    #          拿到 task_id、call_id 后调 recorder.bind(...)（缺口 E-G-8）；文件名 <scenario_id>.jsonl（X-G4）。
    # todo(A): 组装顺序：Store → Trace(store, clock) → registry、Policy.load(registry.policy_raw())（G-G-02）→ DecisionGate
    #          → PendingRegistry → LlmTaskRunner → ResultService(store=, trace=, gate=, llm=, skills=, clock=)（A-G-3）
    #          → CallManager(..., results=)（A-G-8）→ TaskService、AssistantService → AppParts。
    # todo(A): CallManager 的 session_factory：live-sim → call.live.create_live_call；
    #          text-sim / replay → call.textcall.create_text_call（interfaces.CallSessionFactory）。
    # todo(A): LineTokens、HandoffTokens 来自 call.line_ws / call.handoff（D 线实现），TTL 取 engine.handoff.token_ttl_s。
    raise NotImplementedError
