"""B-s5 K1: a peer closing first must not break a known call's hangup."""

import asyncio
import json
from unittest.mock import AsyncMock

import httpx
import pytest
from fastapi import FastAPI
from websockets.exceptions import ConnectionClosedError, ConnectionClosedOK
from websockets.frames import Close

from waitless.company.carrier import CarrierState, build_carrier_router
from waitless.company.config import load_company
from waitless.models import CarrierDialIn


@pytest.mark.parametrize("close_code", [1000, 1011])
async def test_hangup_after_peer_close_completes_and_reaps_task(
    company_dir, tmp_path, mode_env, close_code,
):
    company = load_company(company_dir, "lakeshore-energy")
    state = CarrierState([company])
    state.company_dir = company_dir
    state.company_stats_path = tmp_path / "stats.jsonl"
    env = mode_env("live-sim", company=True)
    dial = CarrierDialIn(
        call_id="closed-line-call", task_id="closed-line-task",
        to_number=company.line_number, stream_url=env["WAITLESS_LINE_WS_URL"],
        stream_token="test-closed-line-token",
    )
    # Queue the real call, then install the closed transport before its task runs.
    result = await state.place_call(dial)
    call = state._calls[result.call_sid]
    task = state._tasks[result.call_sid]
    error_type = ConnectionClosedOK if close_code == 1000 else ConnectionClosedError
    closed = error_type(Close(close_code, "peer closed"), Close(close_code, "peer closed"), True)
    ws = AsyncMock()
    ws.send.side_effect = closed
    call._ws = ws
    call._started = True
    app = FastAPI()
    app.include_router(build_carrier_router(state))
    try:
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app), base_url=env["WAITLESS_CARRIER_URL"],
        ) as client:
            for _ in range(2):
                response = await client.post(f"/carrier/calls/{result.call_sid}/hangup")
                assert response.status_code == 200
                assert response.json() == {"call_sid": result.call_sid, "status": "completed"}
        assert task.done()
        assert result.call_sid not in state._tasks
        ws.send.assert_awaited_once()
        ws.close.assert_awaited_once()
        from tests.lane_B.i19b_stats_support import assert_empty
        row, = [json.loads(line) for line in state.company_stats_path.read_text().splitlines()]
        assert_empty(row, call_id=dial.call_id, call_sid=result.call_sid, by='api_hangup', reason='api')

    finally:
        await state._shutdown()
        if not task.done():
            task.cancel()
        await asyncio.gather(task, return_exceptions=True)
