"""模拟公司进程（B 线；tech §5.4、§6.3）。`waitless company` 启动它，只监听回环地址。

公开接口：create_company_app(settings)、run_company(settings)（cli.py 调用）。
"""

import ipaddress
import os
import sys
from contextlib import asynccontextmanager
from typing import Any

from fastapi import FastAPI, WebSocket

from waitless.config import Settings
from waitless.company.carrier import CarrierState, build_carrier_router
from waitless.company.bot import CompanyBot
from waitless.company.config import ScenarioError, load_company, load_scenario

from waitless.company.scripted import agent_mode_from_env, check_agent_mode, check_scripted_steps

__all__ = ["create_company_app", "run_company"]


def create_company_app(settings: Settings, *, agent_mode: str = "sonic") -> Any:
    """FastAPI：挂 company.carrier.build_carrier_router；公司配置来自 settings.company_dir。
    settings.company_stats_path 有值时，CarrierCall 每通结束往它追加统计行（缺口 X-G6，暂定）。"""
    check_agent_mode(agent_mode)
    root = settings.company_dir
    if not root.is_dir():
        raise ScenarioError(f"Missing company directory: {root}")
    companies = [load_company(root, path.name) for path in sorted(root.iterdir())
                 if path.is_dir() and path.name != "hidden" and not path.name.startswith(".")]
    if not companies:
        raise ScenarioError("Company directory contains no companies")
    for company in companies:
        scenario = load_scenario(root, company.company_id, "baseline")
        if agent_mode == "scripted":
            check_scripted_steps(scenario, company)
    state = CarrierState(companies)
    state.agent_mode = agent_mode
    state.company_dir = root
    state.company_stats_path = settings.company_stats_path
    state._settings = settings

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        try:
            yield
        finally:
            await state._shutdown()

    app = FastAPI(lifespan=lifespan)
    app.state.carrier = state
    app.include_router(build_carrier_router(state))

    @app.websocket("/ws/company-bot")
    async def company_bot(websocket: WebSocket) -> None:
        role = websocket.query_params.get("role")
        call_sid = websocket.query_params.get("call_sid")
        call = state._calls.get(call_sid or "")
        engine = getattr(call, "_engine", None)
        if role not in {"agent", "rep"} or call is None or call._closed or engine is None:
            await websocket.close(code=4401)
            return
        bot = CompanyBot(role=role, engine=engine, settings=settings)
        await websocket.accept()
        try:
            await bot.run(websocket)
        finally:
            if websocket.application_state.name != "DISCONNECTED":
                await websocket.close()

    return app


def run_company(settings: Settings) -> int:
    """uvicorn.run(host=settings.company_host, port=settings.company_port)；host 不是回环地址时拒绝启动（K18）。
    歧义表 A-13（定稿 company_cmd_only_root）：company_host 或 company_port 缺失时，在 stderr 列出缺的变量名
    （config.REQUIRED_FOR_COMPANY）并返回退出码 2，不启动；`waitless serve` 不检查这两个变量。"""
    missing = []
    if not settings.company_host:
        missing.append("WAITLESS_COMPANY_HOST")
    if settings.company_port is None:
        missing.append("WAITLESS_COMPANY_PORT")
    if missing:
        print("Missing configuration: " + ", ".join(missing), file=sys.stderr)
        return 2
    try:
        if not ipaddress.ip_address(settings.company_host).is_loopback:
            raise ValueError("Company host must be a loopback address")
        mode = agent_mode_from_env(os.environ)
        app = create_company_app(settings, agent_mode=mode)
    except (ValueError, ScenarioError) as exc:
        print(str(exc), file=sys.stderr)
        return 2
    import uvicorn
    uvicorn.run(app, host=settings.company_host, port=settings.company_port)
    return 0
