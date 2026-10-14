"""I2B-6: cleanup order, failure isolation and real pipecat process exit."""

import asyncio
import os
import subprocess
import sys
import time
import traceback
from types import SimpleNamespace
from unittest.mock import AsyncMock

import httpx
import pytest

from a_support import AEnv
from waitless.cli import check_live
from .test_r9_live_pending import pending_check


async def test_cleanup_hangs_up_before_stopping_server_and_company(pending_check):
    check = pending_check
    await check_live("baseline", check.settings)
    assert check.order == ["hangup", "server_exit", "company_terminate", "store_close"]
    assert 0 < check.servers[0].config.timeout_graceful_shutdown <= 5
    check.company.wait.assert_awaited_once()
    check.services.store.close.assert_called_once()
    check.carrier.aclose.assert_awaited_once()


async def test_shutdown_continues_after_one_carrier_failure(tmp_path, caplog):
    env = AEnv(tmp_path)
    try:
        manager = env.call_manager()
        first = SimpleNamespace(ended=False, hangup=AsyncMock(side_effect=httpx.ConnectError("carrier unavailable")))
        second = SimpleNamespace(ended=False, hangup=AsyncMock())
        manager._sessions.update(first=first, second=second)
        await manager.shutdown()
        first.hangup.assert_awaited_once_with("shutdown")
        second.hangup.assert_awaited_once_with("shutdown")
        assert "ConnectError" in caplog.text
    finally:
        env.close()


async def test_cleanup_errors_preserve_original_mcp_error(pending_check):
    check = pending_check
    check.handlers.get_call_status = AsyncMock(side_effect=RuntimeError("original MCP failure"))
    check.parts.calls.shutdown.side_effect = RuntimeError("shutdown failure")
    check.carrier.aclose.side_effect = RuntimeError("carrier close failure")
    check.company.terminate.side_effect = ProcessLookupError()
    with pytest.raises(ExceptionGroup) as caught:
        await check_live("baseline", check.settings)
    assert "MCP check failed: Error executing tool get_call_status" in "".join(traceback.format_exception(caught.value))
    check.parts.calls.shutdown.assert_awaited_once()
    check.company.wait.assert_awaited_once()
    check.services.store.close.assert_called_once()


@pytest.mark.parametrize("hangup_fails", [False, True])
def test_real_pipeline_check_exits_within_30_seconds(hangup_fails, tmp_path):
    env = dict(os.environ, TASK_A_R9_PIPELINE_CHILD="1",
               TASK_A_R9_HANGUP_FAILS=str(int(hangup_fails)))
    command = [sys.executable, "-m", "pytest", "-q", "-s", "-p", "no:cacheprovider", "--basetemp", str(tmp_path / "probe"),
               "tests/lane_A/test_r9_live_cleanup.py::test_real_pipeline_child"]
    started_at = time.monotonic()
    try:
        result = subprocess.run(command, env=env, capture_output=True, text=True, timeout=30)
    except subprocess.TimeoutExpired as exc:
        pytest.fail("check_live left the real pipecat pipeline running after its error: " + str(exc))
    assert result.returncode == 0, result.stdout + result.stderr
    assert "PIPELINE_EXITED_AFTER_ORIGINAL_ERROR" in result.stdout, result.stdout + result.stderr
    print(f"real pipecat hangup_fails={hangup_fails}: exit=0 elapsed={time.monotonic() - started_at:.2f}s")


async def test_real_pipeline_child(pending_check):
    if os.environ.get("TASK_A_R9_PIPELINE_CHILD") != "1":
        pytest.skip("subprocess probe only")
    from pipecat.frames.frames import StartFrame
    from pipecat.pipeline.pipeline import Pipeline
    from pipecat.pipeline.runner import PipelineRunner
    from pipecat.pipeline.task import PipelineTask
    from pipecat.processors.frame_processor import FrameProcessor

    started = asyncio.Event()

    class Passthrough(FrameProcessor):
        async def process_frame(self, frame, direction):
            await super().process_frame(frame, direction)
            await self.push_frame(frame, direction)
            if isinstance(frame, StartFrame):
                started.set()

    check = pending_check
    pipe = PipelineTask(Pipeline([Passthrough()]))
    handler = asyncio.create_task(PipelineRunner(handle_sigint=False).run(pipe))
    check.app.state._line_tasks = {handler}
    await asyncio.wait_for(started.wait(), 5)
    check.handlers.get_call_status = AsyncMock(side_effect=RuntimeError("original MCP failure"))

    async def shutdown():
        check.order.append("hangup")
        if os.environ["TASK_A_R9_HANGUP_FAILS"] == "1":
            raise httpx.ConnectError("carrier failed before pipe cancellation")
        await pipe.cancel()

    check.parts.calls.shutdown.side_effect = shutdown
    with pytest.raises(ExceptionGroup) as caught:
        await check_live("baseline", check.settings)
    assert "MCP check failed: Error executing tool get_call_status" in "".join(traceback.format_exception(caught.value))
    assert handler.done(), "line handler must finish before check_live raises"
    assert check.order == ["hangup", "server_exit", "company_terminate", "store_close"]
    print("PIPELINE_EXITED_AFTER_ORIGINAL_ERROR", flush=True)


async def test_app_tracks_only_line_handlers(tmp_path, monkeypatch):
    from waitless import app as app_module
    from waitless.call import line_ws
    env = AEnv(tmp_path)
    try:
        parts = env.parts()
        services = SimpleNamespace(store=env.store, trace=env.trace, calls=parts.calls)
        app = app_module.create_app(env.settings.model_copy(update={"mode": "live-sim"}),
                                    services=services, parts=parts)
        entered = asyncio.Event()
        release = asyncio.Event()

        async def endpoint(websocket):
            assert asyncio.current_task() in app.state._line_tasks
            entered.set()
            await release.wait()

        monkeypatch.setattr(line_ws, "line_endpoint", endpoint)
        route = next(route for route in app.routes if route.path == "/ws/line")
        handler = asyncio.create_task(route.endpoint(SimpleNamespace(app=app)))
        await asyncio.wait_for(entered.wait(), 1)
        assert app.state._line_tasks == {handler}
        release.set()
        await handler
        assert app.state._line_tasks == set()
    finally:
        env.close()


async def test_timeouts_cancel_operations_and_kill_then_reap_company(pending_check, monkeypatch, capsys):
    from waitless.cli import _LiveCheckCleanup
    check = pending_check
    remaining = _LiveCheckCleanup.remaining
    monkeypatch.setattr(_LiveCheckCleanup, "remaining", lambda self, limit: remaining(self, min(limit, 0.03)))
    cancelled = []

    async def stalled_shutdown():
        check.order.append("hangup")
        try:
            await asyncio.Event().wait()
        finally:
            cancelled.append("hangup")

    async def stalled_result(*args, **kwargs):
        try:
            await asyncio.Event().wait()
        finally:
            cancelled.append("result")

    wait_count = 0

    async def company_wait():
        nonlocal wait_count
        wait_count += 1
        if wait_count == 1:
            await asyncio.Event().wait()
        return 0

    check.parts.calls.shutdown.side_effect = stalled_shutdown
    check.handlers.get_case_result = stalled_result
    check.company.wait.side_effect = company_wait
    report = await check_live("baseline", check.settings)
    assert report.outcome == "failed"
    assert report.confirmation_number is None
    assert cancelled == ["hangup", "result"]
    check.company.terminate.assert_called_once()
    check.company.kill.assert_called_once()
    assert check.company.wait.await_count == 2
    check.services.store.close.assert_called_once()
    assert "TimeoutError" in capsys.readouterr().err


async def test_server_failure_and_store_failure_do_not_replace_mcp_error(pending_check, monkeypatch):
    import uvicorn
    check = pending_check
    original_server = uvicorn.Server

    class FailingServer(original_server):
        async def serve(self):
            await super().serve()
            raise RuntimeError("server teardown failure")

    monkeypatch.setattr(uvicorn, "Server", FailingServer)
    check.handlers.get_call_status = AsyncMock(side_effect=RuntimeError("original MCP failure"))
    check.services.store.close.side_effect = RuntimeError("store close failure")
    with pytest.raises(ExceptionGroup) as caught:
        await check_live("baseline", check.settings)
    assert "MCP check failed: Error executing tool get_call_status" in "".join(traceback.format_exception(caught.value))
    check.company.wait.assert_awaited_once()
    check.services.store.close.assert_called_once()


async def test_pending_cleanup_outlives_polling_deadline(pending_check, monkeypatch):
    import yaml
    check = pending_check
    scenario = check.settings.company_dir / "lakeshore-energy" / "scenarios" / "baseline.yaml"
    limit = yaml.safe_load(scenario.read_text())["max_call_seconds"] + 30
    timeout = asyncio.timeout
    monkeypatch.setattr(asyncio, "timeout", lambda seconds: timeout(0.03 if seconds == limit else seconds))

    async def slow_shutdown():
        check.order.append("hangup")
        await asyncio.sleep(0.08)

    check.parts.calls.shutdown.side_effect = slow_shutdown
    report = await check_live("baseline", check.settings)
    assert report.outcome == "needs_follow_up"
    check.parts.calls.shutdown.assert_awaited_once()
    assert check.order == ["hangup", "server_exit", "company_terminate", "store_close"]
