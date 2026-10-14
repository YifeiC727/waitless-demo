from a_support import (
    AEnv, LAKESHORE_REQUEST, PROFILE_VALUES, default_prepare_gate,
    error_payload, mcp_session, write_web,
)


async def test_ask_user_card_is_ready_but_mcp_start_requires_all_answers(tmp_path):
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
            assert all(q["needs_user"] and q["selected"] is None for q in card["precall_decisions"])
            task_id = out["task_id"]

            async def refused(version):
                before = env.store.get_task(task_id), env.store.list_calls(task_id)
                result = await client.call_tool("start_assisted_call", {
                    "task_id": task_id, "confirmed": True, "card_version": version,
                })
                assert error_payload(result)["code"] == "task_not_ready"
                assert (env.store.get_task(task_id), env.store.list_calls(task_id)) == before
                assert env.factory.specs == []

            await refused(card["card_version"])
            updated = await client.call_tool("update_task", {
                "task_id": task_id, "decisions": {"deposit_or_autopay": "autopay"},
            })
            assert not updated.is_error
            out = updated.structured_content
            assert out["status"] == "ready" and out["missing"] == []
            assert any(q["needs_user"] for q in out["task_card"]["precall_decisions"])
            await refused(out["task_card"]["card_version"])

            updated = await client.call_tool("update_task", {
                "task_id": task_id, "decisions": {"bill_delivery": "paper"},
            })
            assert not updated.is_error
            out = updated.structured_content
            assert out["status"] == "ready"
            assert all(not q["needs_user"] and q["selected"] is not None
                       for q in out["task_card"]["precall_decisions"])
            started = await client.call_tool("start_assisted_call", {
                "task_id": task_id, "confirmed": True,
                "card_version": out["task_card"]["card_version"],
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
        updated = await tasks.update(prepared.task_id, decisions={"deposit_or_autopay": "autopay"})
        assert updated.status == "setup_required" and updated.missing == prepared.missing
    finally:
        env.close()
