"""组装 FastAPI 应用（A 线；tech §6.2、§6.3、§6.4、inv-1 §2.1；AC-01、AC-18）。

公开接口（§7）：create_app(settings)。路由注册顺序有要求：先注册所有 FastAPI 路由与静态文件，
最后把 MCP 的 http_app 挂到 "/"（内部路径 /mcp）；MCP 的 session_manager.run() 在父 lifespan 里跑。

缺口 A-G-1（定稿 app_parts_dataclass）：A 线自有的 AppParts 装 MCP 工具与 /assistant/turn 要的 A 线服务；
冻结的 interfaces.AppServices 不动（D 的端点只读它）。create_app 与 ToolHandlers 收同一个 AppParts。
缺口 E-G-3（定稿 build_services_injection）：build_services 可注入 Jev、Converse 客户端与时钟，E、X 的整条链
测试经它组装，不碰 A 的内部方法。
"""

from waitless.official_number import FixtureFetcher

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
    from fastapi import FastAPI, Request, WebSocket
    from fastapi.responses import FileResponse, JSONResponse
    from fastapi.staticfiles import StaticFiles
    from mcp.server.transport_security import TransportSecuritySettings
    from urllib.parse import urlunsplit
    from waitless import __version__
    from waitless.mcp_server import build_mcp, ToolHandlers
    from waitless.call.listen import ListenTicketOut
    from waitless.interfaces import ToolFailure
    from waitless.models import ApiError, AppConfigOut, AssistantTurnIn, AssistantTurnOut, HealthOut, ReplayHeader
    owned_services = None
    if services is None or parts is None:
        built_services, built_parts = build_services(settings)
        owned_services = built_services
        services = services or built_services
        parts = parts or built_parts
    mcp = build_mcp(services, ToolHandlers(services, parts))
    # The SDK matches exact authorities or :* port patterns. Keep both forms
    # so the configured hostnames also accept requests without an explicit port.
    hosts = [authority for host in settings.allowed_hosts for authority in (host, f"{host}:*")]
    origins = [urlunsplit((scheme, host, "", "", "")) for host in hosts for scheme in ("http", "https")]
    http_app = mcp.streamable_http_app(stateless_http=True, json_response=True, streamable_http_path="/mcp",
                 host=settings.host, transport_security=TransportSecuritySettings(
                     enable_dns_rebinding_protection=True, allowed_hosts=hosts, allowed_origins=origins))
    @asynccontextmanager
    async def lifespan(app):
        try:
            async with http_app.router.lifespan_context(http_app):
                try:
                    yield
                finally:
                    await parts.calls.shutdown()
        finally:
            if owned_services:
                try:
                    for resource in getattr(owned_services, "_resources", []):
                        if hasattr(resource, "aclose"):
                            await resource.aclose()
                        elif hasattr(resource, "close"):
                            resource.close()
                finally:
                    owned_services.store.close()
    app = FastAPI(lifespan=lifespan)
    app.state.services = services
    app.state.parts = parts
    app.state.listen_hosts = hosts
    app.state.listen_origins = origins
    @app.get("/", include_in_schema=False)
    async def index():
        return FileResponse(settings.web_dir / "index.html", media_type="text/html")
    app.mount("/js", StaticFiles(directory=settings.web_dir / "js", check_dir=False), name="js")
    @app.get("/app-config", response_model=AppConfigOut)
    async def app_config():
        text = None
        if settings.mode == "replay":
            path = _replay_path(settings)
            with path.open(encoding="utf-8") as source:
                text = ReplayHeader.model_validate_json(source.readline()).request_text
        return AppConfigOut(mode=settings.mode, poll_ms=settings.engine.web.poll_ms, replay_request_text=text)
    @app.get("/healthz", response_model=HealthOut)
    async def health():
        return HealthOut(ok=True, mode=settings.mode, version=__version__)
    @app.post("/assistant/turn", response_model=AssistantTurnOut)
    async def assistant_turn(inp: AssistantTurnIn):
        return await parts.assistant.turn(inp)
    @app.post("/listen", response_model=ListenTicketOut)
    async def listen(request: Request):
        def allowed(value, patterns):
            return bool(value) and (value in patterns or any(
                pattern.endswith(":*") and value.startswith(pattern[:-2] + ":")
                for pattern in patterns))

        def error(status, code, message=""):
            return JSONResponse(status_code=status, content=ApiError(code=code, message=message).model_dump())

        if (not allowed(request.headers.get("host"), app.state.listen_hosts)
                or not allowed(request.headers.get("origin"), app.state.listen_origins)):
            return error(403, "forbidden_origin")
        if request.headers.get("content-type", "").split(";", 1)[0].strip().lower() != "application/json":
            return error(400, "invalid_request")
        try:
            body = await request.json()
        except (ValueError, UnicodeDecodeError):
            return error(400, "invalid_request")
        if not isinstance(body, dict) or not isinstance(body.get("task_id"), str):
            return error(400, "invalid_request")
        try:
            return parts.calls.listen(body["task_id"])
        except ToolFailure as exc:
            return error(404 if exc.code == "unknown_task" else 409, exc.code, exc.message)
    if settings.mode == "live-sim":
        app.state._line_tasks = set()
        @app.websocket("/ws/line")
        async def line(websocket: WebSocket):
            import asyncio
            from waitless.call.line_ws import line_endpoint
            task = asyncio.current_task()
            app.state._line_tasks.add(task)
            try:
                await line_endpoint(websocket)
            finally:
                app.state._line_tasks.discard(task)
        @app.websocket("/ws/listen/{call_id}")
        async def listen_ws(websocket: WebSocket, call_id: str):
            from waitless.call.listen import listen_endpoint
            await listen_endpoint(websocket, call_id)
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
    from waitless.config import SystemClock
    from waitless.store import Store
    from waitless.trace import Trace
    from waitless.skills.loader import load_skills
    from waitless.skills.compiler import compile_call
    from waitless.decision.registry import load_registry
    from waitless.decision.policy import Policy
    from waitless.decision.gate import DecisionGate
    from waitless.decision.pending import PendingRegistry
    from waitless.decision.jev import HttpJevClient
    from waitless.bedrock import build_converse_client
    from waitless.llm_tasks import LlmTaskRunner
    from waitless.call.handoff import HandoffTokens
    from waitless.interfaces import CallDeps
    import yaml
    import json
    clock = clock or SystemClock()
    skills = load_skills(settings.skills_dir)
    registry = load_registry(skills)
    registry.validate()
    player = None
    if settings.mode == "replay":
        from waitless.replay.player import ReplayPlayer
        player = ReplayPlayer(_replay_path(settings), registry=registry)
        jev = jev if jev is not None else player.jev_client()
        converse = converse if converse is not None else player.converse_client()
    else:
        jev = jev if jev is not None else HttpJevClient(url=settings.jev_url, model=settings.jev_model,
                             api_key=settings.jev_api_key, timeout_s=5.0)
        converse = converse if converse is not None else build_converse_client(settings)
    http_jev = jev
    recording = None
    if settings.record_dir is not None:
        if settings.mode != "text-sim" or settings.planner != "bedrock":
            raise ValueError("Formal recording requires text-sim and Bedrock")
        recording = _RecordingClients(settings, registry, skills, clock, jev, converse)
        jev = converse = recording
    store = Store(settings.db_path)
    try:
        for path in sorted(settings.company_dir.glob("*/company.yaml")):
            raw = yaml.safe_load(path.read_text(encoding="utf-8"))
            existing = store.get_company(raw["company_id"])
            store.upsert_company(raw["company_id"], name=raw["name"], line_number=raw["line_number"],
                    official_number=existing["official_number"] if existing else None,
                    sources=json.loads(existing["sources_json"]) if existing else [],
                    verified_at=existing["verified_at"] if existing else None)
        trace = Trace(store, clock)
        policy = Policy.load(registry.policy_raw())
        model = settings.jev_model if settings.mode != "replay" else player.header.models.jev
        gate = DecisionGate(jev=jev, jev_model=model, registry=registry, policy=policy, trace=trace, clock=clock)
        llm_model = settings.planner_model_id
        if settings.mode == "replay":
            llm_model = player.header.models.planner.split(":", 1)[-1]
        elif settings.planner == "codex":
            llm_model = _codex_model(settings)
        llm = LlmTaskRunner(converse, model_id=llm_model)
        pending = PendingRegistry(clock)
        line_tokens = _LazyLineTokens(clock)
        handoff_tokens = HandoffTokens(clock, ttl_s=settings.engine.handoff.token_ttl_s)
        results = ResultService(store=store, trace=trace, gate=gate, llm=llm, skills=skills, clock=clock)
        async def on_ended(call_id):
            await calls.on_ended(call_id)
        deps = CallDeps(settings=settings, store=store, trace=trace, gate=gate, pending=pending, llm=llm,
                       clock=clock, line_tokens=line_tokens, handoff_tokens=handoff_tokens, on_ended=on_ended)
        if settings.mode == "live-sim":
            from waitless.call.live import create_live_call
            factory = create_live_call
        else:
            from waitless.call.textcall import create_text_call
            factory = create_text_call
        calls = CallManager(settings=settings, store=store, trace=trace,
                            compile_call=compile_call, session_factory=factory,
                            deps=deps, results=results, replay_scenario_id=player.header.scenario_id if player else None)
        fetcher = FixtureFetcher.from_company_dir(settings.company_dir)
        trusted = TrustedSources.model_validate(yaml.safe_load((skills["find-official-number"].path /
                                  "references/trusted-sources.yaml").read_text(encoding="utf-8")))
        from waitless.utility_lookup import AnthropicWebSearcher, LiveLookup, load_live_lookup_config
        lookup = None
        if settings.lookup == "anthropic":
            config = load_live_lookup_config(skills["find-official-number"])
            lookup = LiveLookup(searcher=AnthropicWebSearcher(
                api_key=settings.anthropic_api_key.get_secret_value(), base_url=settings.anthropic_base_url,
                timeout_s=settings.lookup_timeout_s, model=settings.lookup_model_id, config=config),
                jev=http_jev, config=config, timeout_s=settings.lookup_timeout_s)
        tasks = TaskService(store=store, trace=trace, gate=gate, llm=llm, skills=skills, fetcher=fetcher, clock=clock,
                            lookup=lookup)
        assistant = AssistantService(gate=gate, llm=llm, skills=skills, trace=trace, clock=clock)
        backend = recording or player
        if backend:
            tasks._bind_backend = backend.bind
            calls._bind_backend = backend.bind
            if recording:
                tasks._begin_recording = recording.begin
                assistant._begin_recording = recording.begin
        parts = AppParts(tasks=tasks, calls=calls, results=results, assistant=assistant, fetcher=fetcher, trusted_sources=trusted)
        services = AppServices(settings=settings, store=store, trace=trace, clock=clock, calls=calls,
                               line_tokens=line_tokens, handoff_tokens=handoff_tokens)
        services._parts = parts
        services._resources = [obj for obj in (recording, http_jev) if obj is not None]
        return services, parts
    except BaseException:
        store.close()
        raise


def _replay_path(settings):
    from pathlib import Path
    name = settings.replay_file or "baseline-web.jsonl"
    if Path(name).name != name or name in (".", ".."):
        raise ValueError("WAITLESS_REPLAY_FILE must be a filename")
    root = settings.replay_dir.resolve()
    path = (root / name).resolve()
    if not path.is_relative_to(root):
        raise ValueError("Replay file escapes WAITLESS_REPLAY_DIR")
    return path


def _codex_model(settings):
    command = settings.codex_command or ()
    for i, arg in enumerate(command):
        if arg in ("--model", "-m") and i+1 < len(command):
            return command[i+1]
        if arg.startswith("--model="):
            return arg.partition("=")[2]
    return command[0] if command else "codex"


class _RecordingClients:
    """Delay the header until the first user request; record only explicit text-sim runs."""

    def __init__(self, settings, registry, skills, clock, jev, converse):
        self.settings, self.registry, self.skills, self.clock = settings, registry, skills, clock
        self.jev, self.planner = jev, converse
        self.recorder = None
        self.ids = {}

    def begin(self, text):
        if self.recorder is not None:
            return
        from waitless.models import ReplayHeader, ReplayModels, SkillCommit
        from waitless.replay.recorder import ReplayRecorder
        scenario = self.settings.engine.textsim.default_scenario
        root = self.settings.record_dir
        root.mkdir(parents=True, exist_ok=True)
        path = root / (scenario + ".jsonl")
        if path.exists():
            raise FileExistsError("Recording already exists")
        header = ReplayHeader(replay_version=1, recorded_at=self.clock.now_iso(), source_mode="text-sim",
                 scenario_id=scenario, request_text=text,
                 skills=[SkillCommit(name=s.name, commit=s.version.commit) for s in self.skills.values()],
                 models=ReplayModels(jev=self.settings.jev_model, planner="bedrock:" + self.settings.planner_model_id))
        self.recorder = ReplayRecorder(path, header, registry=self.registry)
        self.recorder.bind(**self.ids)
        self.jev = self.recorder.wrap_jev(self.jev)
        self.planner = self.recorder.wrap_converse(self.planner)

    def bind(self, *, task_id=None, call_id=None):
        self.ids.update({k:v for k,v in {"task_id":task_id,"call_id":call_id}.items() if v is not None})
        if self.recorder:
            self.recorder.bind(task_id=task_id, call_id=call_id)

    async def ask(self, state, questions):
        if self.recorder is None:
            raise RuntimeError("Recording has no user request")
        return await self.jev.ask(state, questions)

    async def converse(self, call):
        if self.recorder is None:
            raise RuntimeError("Recording has no user request")
        return await self.planner.converse(call)

    def close(self):
        if self.recorder:
            self.recorder.close()


class _LazyLineTokens:
    # Text mode never issues line tokens and must not import the audio serializer.
    def __init__(self, clock):
        self.clock = clock
        self.tokens = None

    def _get(self):
        if self.tokens is None:
            from waitless.call.line_ws import LineTokens
            self.tokens = LineTokens(self.clock)
        return self.tokens

    def issue(self, call_id):
        return self._get().issue(call_id)

    def verify(self, token):
        return self._get().verify(token)

    def revoke(self, call_id):
        if self.tokens:
            self.tokens.revoke(call_id)
