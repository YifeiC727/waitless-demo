"""AC-32：profile 表没有 ssn、card_number、date_of_birth 列；通过 Store 或 update_task 写这些键时返回 forbidden_field。

机制（tech §7.1）：Store.save_profile() 写之前先整体检查键，命中 FORBIDDEN_FIELD_KEYS 就抛 ForbiddenField，什么都不写；
update_task 把它转成 ToolFailure("forbidden_field")（定稿 A-05：UpdateTaskFields 允许未知键进入，由 update_task 拒绝）；
mcp_server 再转成 isError。诱饵值来自 conftest 的 decoy_sensitive，断言它们没有落进数据库文件。
"""

import sqlite3

import pytest

from waitless import models as m
from waitless.interfaces import ToolFailure
from waitless.mcp_server import build_mcp
from waitless.store import ForbiddenField

from a_support import AEnv, StubHandlers, db_bytes, default_prepare_gate, error_payload, make_settings, mcp_app_for, mcp_session, stub_services, write_web


@pytest.fixture
def env(tmp_path):
    e = AEnv(tmp_path)
    yield e
    e.close()


def test_ac32_profile_table_has_no_forbidden_columns(env):
    con = sqlite3.connect(env.settings.db_path)
    try:
        cols = {row[1] for row in con.execute("PRAGMA table_info(profile)")}
    finally:
        con.close()
    assert cols, "profile table missing"
    assert not (cols & set(m.FORBIDDEN_FIELD_KEYS))


@pytest.mark.parametrize("key", m.FORBIDDEN_FIELD_KEYS)
def test_ac32_store_rejects_forbidden_key_and_writes_nothing(env, decoy_sensitive, key):
    with pytest.raises(ForbiddenField) as err:
        env.store.save_profile({"name": "Alex Example", key: decoy_sensitive[key]})
    assert key in err.value.keys
    assert env.store.get_profile().name is None, "nothing may be written when a forbidden key is present"
    assert decoy_sensitive[key].encode() not in db_bytes(env.settings.db_path)


async def test_ac32_update_task_returns_forbidden_field(env, decoy_sensitive):
    task_id = env.add_task("task-32", status="ready")
    tasks = env.task_service(gate=default_prepare_gate())
    before = env.store.get_task(task_id)
    fields = m.UpdateTaskFields(name="Alex Example", **decoy_sensitive)
    with pytest.raises(ToolFailure) as err:
        await tasks.update(task_id, fields=fields)
    assert err.value.code == "forbidden_field"
    assert env.store.get_profile().name is None
    assert env.store.get_task(task_id) == before
    raw = db_bytes(env.settings.db_path)
    for value in decoy_sensitive.values():
        assert value.encode() not in raw


async def test_ac32_mcp_real_handler_returns_forbidden_field(env, decoy_sensitive):
    """官方客户端 → create_app 的 /mcp → ToolHandlers → TaskService（r3 缺口 A-G-1 的 AppParts）。"""
    task_id = env.add_task("task-32-mcp", status="ready")
    write_web(env.settings.web_dir)
    async with mcp_session(env.app()) as client:
        result = await client.call_tool("update_task", {"task_id": task_id,
                                                        "fields": {"name": "Alex Example", "ssn": decoy_sensitive["ssn"]}})
    assert error_payload(result)["code"] == "forbidden_field"
    assert env.store.get_profile().name is None
    assert decoy_sensitive["ssn"].encode() not in db_bytes(env.settings.db_path)


async def test_ac32_mcp_update_task_forbidden_field_is_is_error(tmp_path, decoy_sensitive):
    handlers = StubHandlers(fail={"update_task": ToolFailure("forbidden_field", "ssn is never stored")})
    server = build_mcp(stub_services(make_settings(tmp_path)), handlers=handlers)
    async with mcp_session(mcp_app_for(server)) as client:
        result = await client.call_tool("update_task", {"task_id": "task-1", "fields": {"ssn": decoy_sensitive["ssn"]}})
    assert error_payload(result)["code"] == "forbidden_field"
    # 定稿 A-05：fields 里的未知键必须到达处理函数（不在 SDK 入参校验阶段被丢掉或报通用错误）
    [call] = handlers.calls_for("update_task")
    assert call["fields"] is not None
    assert (call["fields"].model_extra or {}).get("ssn") == decoy_sensitive["ssn"]
