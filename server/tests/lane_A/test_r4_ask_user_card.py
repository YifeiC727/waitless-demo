import pytest

from a_support import (
    AEnv, LAKESHORE_REQUEST, PROFILE_VALUES, default_prepare_gate,
    error_payload, mcp_session, write_web,
)
from waitless.interfaces import ToolFailure


async def test_ask_user_pick_stays_off_the_card_and_start_is_allowed(tmp_path):
    env = AEnv(tmp_path)
    try:
        env.store.save_profile(PROFILE_VALUES, company_id="lakeshore-energy")
        write_web(env.settings.web_dir)
        async with mcp_session(env.app(gate=default_prepare_gate(precall="ask_user"))) as client:
            prepared = await client.call_tool("prepare_task", {"request": LAKESHORE_REQUEST})
            assert not prepared.is_error
            out = prepared.structured_content
            assert out["status"] == "ready" and out["missing"] == []
            card = out["task_card"]
            assert card["precall_decisions"] == []
            task_id = out["task_id"]
            updated = await client.call_tool("update_task", {
                "task_id": task_id, "decisions": {"deposit_or_autopay": "autopay"},
            })
            assert updated.is_error
            assert error_payload(updated)["code"] == "invalid_option"
            started = await client.call_tool("start_assisted_call", {
                "task_id": task_id, "confirmed": True,
                "card_version": card["card_version"],
            })
            assert not started.is_error and started.structured_content["status"] == "dialing"
            assert len(env.factory.specs) == 1
    finally:
        env.close()


async def test_missing_fields_still_require_setup_with_ask_user(tmp_path):
    env = AEnv(tmp_path)
    try:
        tasks = env.task_service(gate=default_prepare_gate(precall="ask_user"))
        prepared = await tasks.prepare(LAKESHORE_REQUEST)
        assert prepared.status == "setup_required" and prepared.missing
        assert prepared.task_card.precall_decisions == []
        with pytest.raises(ToolFailure) as exc:
            await tasks.update(prepared.task_id, decisions={"deposit_or_autopay": "autopay"})
        assert exc.value.code == "invalid_option"
    finally:
        env.close()
