"""E 第二批 E2a 线内自测（只放第 3 步可见组没有覆盖、但本单要求的细节）：回放文件损坏；
给 E2b 用的两个回放核心接缝（记住第一个 ReplayMismatch、指出第一条剩余记录）。

共用设施取第 3 步的 e_support（冻结，不改）。零密钥零网络。"""

import pytest

from e_support import StubRegistry, replay_header
from e_support.replayfiles import ROUND_QIDS, jev_state, record_line, round_response, said_questions, write_replay
from waitless import models as m
from waitless.interfaces import ReplayMismatch

ROUND_SHAPE = m.ReplayShape(point="call_round", question_ids=ROUND_QIDS)
SAID_SHAPE = m.ReplayShape(point="said_as_decided", question_ids=["said"])
SUMMARY_SHAPE = m.ReplayShape(task="write_summary")


def _summary_response() -> dict:
    return {
        "output": {"message": {"role": "assistant", "content": [{"text": "Done."}]}},
        "stopReason": "end_turn",
        "usage": {"inputTokens": 0, "outputTokens": 0, "totalTokens": 0},
        "metrics": {"latencyMs": 0},
    }


def test_player_reports_a_malformed_header_as_replay_mismatch(tmp_path):
    from waitless.replay.player import ReplayPlayer

    path = tmp_path / "bad.jsonl"
    path.write_text("not json\n", encoding="utf-8")
    with pytest.raises(ReplayMismatch) as err:
        ReplayPlayer(path, registry=StubRegistry()).next("jev", ROUND_SHAPE)
    assert err.value.channel and err.value.seq >= 0
    assert path.read_text(encoding="utf-8") == "not json\n"


def test_player_reports_a_malformed_record_as_replay_mismatch(tmp_path):
    from waitless.replay.player import ReplayPlayer

    path = tmp_path / "bad-record.jsonl"
    path.write_text(replay_header().model_dump_json() + "\n" + '{"channel": "jev", "seq": "x"}\n', encoding="utf-8")
    with pytest.raises(ReplayMismatch) as err:
        ReplayPlayer(path, registry=StubRegistry()).next("jev", ROUND_SHAPE)
    assert err.value.channel and err.value.seq >= 0


async def test_player_remembers_only_the_first_mismatch(tmp_path):
    from waitless.replay.player import ReplayPlayer

    path = write_replay(tmp_path / "one.jsonl", replay_header(),
                        [record_line("jev", 0, ROUND_SHAPE.model_dump(exclude_none=True), round_response("1"))])
    player = ReplayPlayer(path, registry=StubRegistry())
    assert player._first_mismatch is None
    with pytest.raises(ReplayMismatch) as first:
        await player.jev_client().ask(jev_state(), said_questions())
    with pytest.raises(ReplayMismatch):
        player.next("converse:write_summary", SUMMARY_SHAPE)
    assert player._first_mismatch is first.value
    assert (player._first_mismatch.channel, player._first_mismatch.seq) == ("jev", 0)


def test_player_names_the_first_leftover_record_in_file_order(tmp_path):
    from waitless.replay.player import ReplayPlayer

    path = write_replay(
        tmp_path / "three.jsonl",
        replay_header(),
        [
            record_line("jev", 0, ROUND_SHAPE.model_dump(exclude_none=True), round_response("1")),
            record_line("converse:write_summary", 0, {"task": "write_summary"}, _summary_response()),
            record_line("jev", 1, ROUND_SHAPE.model_dump(exclude_none=True), round_response("2")),
        ],
    )
    player = ReplayPlayer(path, registry=StubRegistry())
    assert player._first_remaining() == ("jev", 0)
    player.next("jev", ROUND_SHAPE)
    assert player._first_remaining() == ("converse:write_summary", 0)
    player.next("jev", ROUND_SHAPE)
    assert player._first_remaining() == ("converse:write_summary", 0)
    player.next("converse:write_summary", SUMMARY_SHAPE)
    assert player._first_remaining() is None and player.remaining() == 0
