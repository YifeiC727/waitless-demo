"""B 线 · 音频部分（10/15）：公司方 bot 的接口面（tech §6.10 末段、§6.3 WS /ws/company-bot；K14；I-2 的 AC-95）。

公司 bot 真正跑 Nova Sonic 要密钥，在 I-2 联调（见 test_i2_live_polly.py 与测试计划的检查命令）。
默认测试只检查不需要模型的部分：公司侧工具清单，以及 /ws/company-bot 对坏参数的拒绝。
"""

import asyncio

import pytest

from waitless.company.app import create_company_app
from waitless.company.bot import COMPANY_TOOLS

from ._live_harness import company_settings, loopback_host, no_proxy, patch_boto3_polly, running_company


def test_ac95_company_tools_match_spec() -> None:
    assert COMPANY_TOOLS == (
        "record_detail",
        "verify_account",
        "transfer_to_representative",
        "request_account_holder",
        "account_holder_verified",
        "present_offer",
        "record_answer",
        "issue_confirmation",
        "end_call",
    )


async def _expect_rejected(url: str) -> None:
    from websockets.asyncio.client import connect
    from websockets.exceptions import ConnectionClosed, InvalidStatus

    try:
        async with connect(url, proxy=None, open_timeout=5) as ws:
            await asyncio.wait_for(ws.recv(), 5)
    except (InvalidStatus, ConnectionClosed):
        return
    except TimeoutError as exc:
        raise AssertionError(f"{url} was accepted and left open; it must be refused") from exc
    raise AssertionError(f"{url} sent a message instead of refusing the connection")


@pytest.mark.parametrize(
    "query",
    [
        "role=boss&call_sid=CA" + "0" * 32,  # role 只能是 agent 或 rep
        "role=agent&call_sid=CA" + "0" * 32,  # 没有这通电话
        "role=rep",  # 缺 call_sid
    ],
)
async def test_ac95_company_bot_ws_refuses_bad_parameters(
    query: str, monkeypatch: pytest.MonkeyPatch, mode_env, engine_config, company_dir, tmp_path  # type: ignore[no-untyped-def]
) -> None:
    from fakes import FakePolly

    patch_boto3_polly(monkeypatch, FakePolly())
    env = mode_env("live-sim", company=True)
    host = loopback_host(env)
    no_proxy(monkeypatch, host)
    app = create_company_app(company_settings(env, engine_config, company_dir, tmp_path))
    async with running_company(app, host) as base:
        await _expect_rejected(base.replace("http://", "ws://") + "/ws/company-bot?" + query)
