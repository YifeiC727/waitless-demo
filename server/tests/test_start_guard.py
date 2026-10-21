"""AC-24：confirmed 不为 true，或 card_version 不是当前版本时，start_assisted_call 返回 isError
（code 为 start_not_confirmed 或 card_version_mismatch），任务状态和 calls 表都不变。

机制（tech §7.1）：CallManager.start() 先核对 confirmed 与 card_version，再写库或拨号；mcp_server 把 ToolFailure
转成 isError=true、content[0].text={"code","message"}（§6.1 公共约定）。r3 缺口 R2-1：未知 task_id 返回 unknown_task，
且在核对 confirmed 之前。r3 缺口 A-G-1：真实处理函数经 AppParts 接到 MCP 上（create_app(settings, services, parts)）。
通话实现（D/E 线）用 FakeSessionFactory，G 线 compile_call 用 RecordingCompile。
"""

import json

import pytest

from waitless.interfaces import ToolFailure
from waitless.mcp_server import build_mcp, tool_error_result

from a_support import AEnv, StubHandlers, error_payload, make_settings, mcp_app_for, mcp_session, stub_services, write_web


@pytest.fixture
def env(tmp_path):
    e = AEnv(tmp_path)
    yield e
    e.close()


def _snapshot(env, task_id):
    return env.store.get_task(task_id), env.store.list_calls(task_id)


@pytest.mark.parametrize(
    ("confirmed", "card_version", "code"),
    [
        (False, 2, "start_not_confirmed"),
        (True, 1, "card_version_mismatch"),  # 用户确认的是旧卡
        (True, 3, "card_version_mismatch"),  # 不存在的新版本
    ],
)
async def test_ac24_guard_rejects_without_side_effects(env, confirmed, card_version, code):
    task_id = env.add_task("task-24", version=2, status="ready")
    calls = env.call_manager()
    before = _snapshot(env, task_id)
    with pytest.raises(ToolFailure) as err:
        await calls.start(task_id, confirmed, card_version)
    assert err.value.code == code
    assert _snapshot(env, task_id) == before
    assert env.store.list_calls(task_id) == []
    assert env.factory.specs == [], "no call session may be created (nothing dialed)"


async def test_ac24_confirmed_current_card_starts_one_call(env):
    """对照：两项都对时才建通话、写 calls 行、任务进入 in_call。"""
    task_id, version = await env.ready_task()
    calls = env.call_manager()
    out = await calls.start(task_id, True, version)
    assert out.task_id == task_id and out.status == "dialing"
    assert len(env.factory.specs) == 1 and env.factory.specs[0].call_id == out.call_id
    assert env.factory.sessions[0].started is True
    rows = env.store.list_calls(task_id)
    assert [r["call_id"] for r in rows] == [out.call_id]
    assert env.store.get_task(task_id)["status"] == "in_call"


async def test_ac24_unknown_task_is_unknown_task_before_the_confirmation_check(env):
    calls = env.call_manager()
    with pytest.raises(ToolFailure) as err:
        await calls.start("task-that-does-not-exist", False, 1)
    assert err.value.code == "unknown_task"
    assert env.factory.specs == []


async def test_ac24_mcp_real_handler_rejects_unconfirmed_start(env):
    """官方客户端 → create_app 的 /mcp → ToolHandlers → CallManager：被拒时 isError，任务与 calls 表不变。"""
    task_id = env.add_task("task-24-mcp", version=2, status="ready")
    write_web(env.settings.web_dir)
    before = _snapshot(env, task_id)
    async with mcp_session(env.app()) as client:
        refused = await client.call_tool("start_assisted_call", {"task_id": task_id, "confirmed": False, "card_version": 2})
        stale = await client.call_tool("start_assisted_call", {"task_id": task_id, "confirmed": True, "card_version": 1})
    assert error_payload(refused)["code"] == "start_not_confirmed"
    assert error_payload(stale)["code"] == "card_version_mismatch"
    assert _snapshot(env, task_id) == before
    assert env.factory.specs == []


def test_ac24_tool_error_result_carries_code_and_message():
    result = tool_error_result(ToolFailure("card_version_mismatch", "the task card changed"))
    assert result.is_error is True
    assert json.loads(result.content[0].text) == {"code": "card_version_mismatch", "message": "the task card changed"}


async def test_ac24_mcp_start_failure_is_is_error_with_code(tmp_path):
    handlers = StubHandlers(fail={"start_assisted_call": ToolFailure("start_not_confirmed", "user has not confirmed")})
    server = build_mcp(stub_services(make_settings(tmp_path)), handlers=handlers)
    async with mcp_session(mcp_app_for(server)) as client:
        result = await client.call_tool("start_assisted_call", {"task_id": "task-1", "confirmed": False, "card_version": 2})
    assert error_payload(result) == {"code": "start_not_confirmed", "message": "user has not confirmed"}
    assert result.structured_content is None
