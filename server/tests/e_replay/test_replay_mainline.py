"""AC-88：清空 AWS 和 Jev 变量后，replay 模式跑 baseline 主线成功：outcome=confirmed，确认号匹配 ^LE-\\d{5}$（可见组，冻结）。

第二组（10/16–10/18，回放先做）。本线写 E 侧：
- ReplayPlayer 实现 JevClient 与 ConverseClient，按通道 FIFO 取记录、替换占位符（单元层，只用替身）；
- ReplayRecorder 包住真实客户端录下 JSON（单元层）；
- 录制一通 text-sim baseline 再用回放跑同一通：只靠回放文件也走到 confirmed（整条链，等 A、B、G 合并后变绿）。
A 线的装配（load_settings 在 replay 模式不要求密钥、create_app 注入 ReplayPlayer）和 I-1 录制的 replays/baseline.jsonl
由 A 线测试、test_e2e_mcp_replay.py 与测试计划里的检查命令覆盖。
conftest 已在每个测试前清掉 WAITLESS_*、AWS 凭证与 Jev 密钥，并拦住一切出站连接。
"""

import os
import re

from e_support import StubRegistry, replay_header
from e_support.replayfiles import (
    ROUND_QIDS,
    jev_state,
    offer_call,
    read_replay,
    record_line,
    round_questions,
    round_response,
    said_questions,
    said_response,
    summary_call,
    write_replay,
)
from waitless import models as m
from waitless.interfaces import ConverseClient, JevClient


def _player(path):
    from waitless.replay.player import ReplayPlayer

    return ReplayPlayer(path, registry=StubRegistry())


def _offer_response(tool: str) -> dict:
    return {
        "output": {"message": {"role": "assistant", "content": [{"toolUse": {"toolUseId": "t-1", "name": tool, "input": {"options": [{"label": "November 3 at no charge", "detail": "free"}]}}}]}},
        "stopReason": "tool_use",
        "usage": {"inputTokens": 0, "outputTokens": 0, "totalTokens": 0},
        "metrics": {"latencyMs": 0},
    }


def _text_response(text: str) -> dict:
    return {
        "output": {"message": {"role": "assistant", "content": [{"text": text}]}},
        "stopReason": "end_turn",
        "usage": {"inputTokens": 0, "outputTokens": 0, "totalTokens": 0},
        "metrics": {"latencyMs": 0},
    }


def _three_channel_file(tmp_path):
    header = replay_header()
    records = [
        record_line("jev", 0, {"point": "call_round", "question_ids": ROUND_QIDS}, round_response("1")),
        record_line("converse:extract_offer_options", 0, {"task": "extract_offer_options", "tool": "extract_offer_options_tool"}, _offer_response("extract_offer_options_tool")),
        record_line("jev", 1, {"point": "said_as_decided", "question_ids": ["said"]}, said_response(0.96)),
        record_line("converse:write_summary", 0, {"task": "write_summary"}, _text_response("Service start confirmed for task {{task_id}} on call {{call_id}}.")),
    ]
    return header, write_replay(tmp_path / "replays" / "unit.jsonl", header, records)


def test_ac88_player_serves_records_per_channel_in_order(tmp_path):
    header, path = _three_channel_file(tmp_path)
    player = _player(path)

    assert player.header == header
    assert player.remaining() == 4
    assert player.remaining("jev") == 2
    first = player.next("jev", m.ReplayShape(point="call_round", question_ids=ROUND_QIDS))
    offer = player.next("converse:extract_offer_options", m.ReplayShape(task="extract_offer_options", tool="extract_offer_options_tool"))
    second = player.next("jev", m.ReplayShape(point="said_as_decided", question_ids=["said"]))
    assert m.JevResponse.model_validate(first) == m.JevResponse.model_validate(round_response("1"))
    assert offer == _offer_response("extract_offer_options_tool")
    assert m.JevResponse.model_validate(second) == m.JevResponse.model_validate(said_response(0.96))
    assert player.remaining("jev") == 0
    assert player.remaining() == 1


async def test_ac88_player_clients_implement_jev_and_converse(tmp_path):
    _, path = _three_channel_file(tmp_path)
    player = _player(path)
    jev = player.jev_client()
    converse = player.converse_client()
    assert isinstance(jev, JevClient)
    assert isinstance(converse, ConverseClient)

    # A-06：签名不变，决策点由 registry.point_of(question_ids) 反推，调用方不传 point
    got = await jev.ask(jev_state(), round_questions())
    assert got == m.JevResponse.model_validate(round_response("1"))
    offer = await converse.converse(offer_call("extract_offer_options_tool"))
    assert offer == _offer_response("extract_offer_options_tool")
    said = await jev.ask(jev_state("Waitless: No thank you."), said_questions())
    assert said == m.JevResponse.model_validate(said_response(0.96))


async def test_ac88_player_substitutes_placeholders(tmp_path):
    header = replay_header()
    path = write_replay(
        tmp_path / "replays" / "ids.jsonl",
        header,
        [record_line("converse:write_summary", 0, {"task": "write_summary"}, _text_response("Service start confirmed for task {{task_id}} on call {{call_id}}."))],
    )
    player = _player(path)
    player.bind(task_id="task-now-7", call_id="call-now-8")
    out = await player.converse_client().converse(summary_call())
    text = out["output"]["message"]["content"][0]["text"]
    assert text == "Service start confirmed for task task-now-7 on call call-now-8."


async def test_ac88_recorder_round_trips_through_player(tmp_path):
    from fakes import FakeConverse, FakeJev, converse_text
    from waitless.replay.player import ReplayPlayer
    from waitless.replay.recorder import ReplayRecorder

    live_task, live_call = "task-live-42", "call-live-43"
    fake_jev = FakeJev([round_response("1")])
    fake_converse = FakeConverse({"write_summary": [converse_text(f"Done for {live_task} on {live_call}.")]})
    path = tmp_path / "replays" / "baseline.jsonl"
    header = replay_header()
    rec = ReplayRecorder(path, header, registry=StubRegistry())
    rec.bind(task_id=live_task, call_id=live_call)  # E-G-8：拿到 id 之后才告诉录制器
    jev = rec.wrap_jev(fake_jev)
    converse = rec.wrap_converse(fake_converse)
    assert isinstance(jev, JevClient) and isinstance(converse, ConverseClient)

    answered = await jev.ask(jev_state(), round_questions())
    summary = await converse.converse(summary_call())
    rec.close()

    # 包装层把真实回应原样交回
    assert answered == fake_jev.calls[0].response
    assert summary["output"]["message"]["content"][0]["text"] == f"Done for {live_task} on {live_call}."

    got_header, records, raw = read_replay(path)
    assert got_header == header
    assert [r.channel for r in records] == ["jev", "converse:write_summary"]
    assert records[0].shape.point == "call_round"
    assert set(records[0].shape.question_ids or []) == set(ROUND_QIDS)
    assert records[1].shape.task == "write_summary" and records[1].shape.tool is None
    assert live_task not in raw and live_call not in raw
    assert "{{task_id}}" in raw and "{{call_id}}" in raw
    assert got_header.request_text == header.request_text and got_header.scenario_id == "baseline"

    player = ReplayPlayer(path, registry=StubRegistry())
    player.bind(task_id="task-replay-1", call_id="call-replay-2")
    assert await player.jev_client().ask(jev_state(), round_questions()) == answered
    again = await player.converse_client().converse(summary_call())
    assert again["output"]["message"]["content"][0]["text"] == "Done for task-replay-1 on call-replay-2."
    assert player.remaining() == 0


async def test_ac88_record_then_replay_baseline_reaches_confirmed(tmp_path, mode_env, engine_config, test_profile, test_account):
    """AC-88 原文：清空 AWS 和 Jev 变量后，replay 模式跑 baseline 主线成功：outcome=confirmed，确认号匹配 ^LE-\\d{5}$。

    先用 text-sim 按主线录一份 replays/baseline.jsonl（录制器包住两个替身，拿到 id 后 bind，E-G-8；文件名
    <scenario_id>.jsonl，X-G4）；再在 replay 模式下由 build_services 按 settings.replay_dir / replay_file 建 ReplayPlayer，
    不注入任何客户端，按同一条主线再跑一遍。conftest 已清掉密钥并拦住出站连接。"""
    from e_support import build_settings
    from e_support.integration import build_stack, run_scenario, scenario_settings
    from waitless.replay.recorder import ReplayRecorder

    replay_dir = tmp_path / "replays"
    replay_path = replay_dir / "baseline.jsonl"
    recorders: list = []

    def recording(fake_jev, fake_converse, registry):
        rec = ReplayRecorder(replay_path, replay_header(scenario_id="baseline"), registry=registry)
        recorders.append(rec)
        return rec.wrap_jev(fake_jev), rec.wrap_converse(fake_converse)

    def bind(task_id, call_id):
        recorders[0].bind(task_id=task_id, call_id=call_id)

    first = build_stack(
        scenario_settings(build_settings, mode_env, engine_config, tmp_path / "record"),
        scenario_id="baseline", profile=test_profile, account=test_account, clients=recording,
    )
    recorded = await run_scenario(first, "baseline", on_ids=bind)
    recorders[0].close()
    assert recorded.case.outcome == "confirmed"
    assert recorded.task_id not in replay_path.read_text(encoding="utf-8"), "ids are stored as placeholders"

    for name in ("AWS_ACCESS_KEY_ID", "AWS_SECRET_ACCESS_KEY", "WAITLESS_JEV_API_KEY"):
        assert name not in os.environ
    settings = scenario_settings(
        build_settings, mode_env, engine_config, tmp_path / "replay", mode="replay", WAITLESS_REPLAY_DIR=str(replay_dir)
    ).model_copy(update={"replay_file": replay_path.name})
    assert settings.mode == "replay" and settings.aws_access_key_id is None and settings.jev_api_key is None
    second = build_stack(settings, scenario_id="baseline", profile=test_profile, account=test_account, clients=lambda *_: (None, None))
    replayed = await run_scenario(second, "baseline")

    assert replayed.case.outcome == "confirmed"
    assert replayed.case.confirmation_number is not None
    assert re.fullmatch(r"LE-\d{5}", replayed.case.confirmation_number)
    assert second.fake_jev.calls == [] and second.fake_converse.calls == []

    def chain(run):
        return [((e.policy_decision or {}).get("intent"), e.decision_point) for e in run.task_events if e.event_type == "decision"]

    assert chain(replayed) == chain(recorded)
