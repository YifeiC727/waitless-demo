"""Failure-path cleanup for the company bot and the outer carrier line."""
import asyncio
from unittest.mock import AsyncMock

import pytest

from waitless.call.line_protocol import new_call_sid
from waitless.company.carrier import CarrierCall
from waitless.company.config import load_company
from waitless.models import CarrierDialIn


@pytest.mark.parametrize("failure_at", ["send", "close"])
async def test_bot_close_failure_still_stops_and_closes_engine_line(
    company_dir, mode_env, failure_at,
):
    company = load_company(company_dir, "lakeshore-energy")
    env = mode_env("live-sim", company=True)
    call = CarrierCall(
        dial=CarrierDialIn(call_id="cleanup-call", task_id="cleanup-task",
            to_number=company.line_number, stream_url=env["WAITLESS_LINE_WS_URL"],
            stream_token="cleanup-test-token"),
        call_sid=new_call_sid(), scenario_id="baseline", company=company,
    )
    line, bot = AsyncMock(), AsyncMock()
    getattr(bot, failure_at).side_effect = RuntimeError("bot transport failed")
    call._ws, call._bot_ws, call._started = line, bot, True
    reader = asyncio.create_task(asyncio.Event().wait())
    call._bot_receive = reader
    with pytest.raises(RuntimeError, match="bot transport failed"):
        await call.hangup()
    assert reader.done()
    bot.close.assert_awaited_once()
    line.close.assert_awaited_once()
    assert len(line.send.await_args_list) == 1
    from waitless.call.line_protocol import StopMsg, parse_line_message
    stop = parse_line_message(line.send.await_args.args[0], direction="to_engine")
    assert isinstance(stop, StopMsg) and stop.stop.callSid == call.call_sid
    await call.hangup()
    line.send.assert_awaited_once()
    line.close.assert_awaited_once()
