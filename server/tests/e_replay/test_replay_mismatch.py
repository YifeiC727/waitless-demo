"""AC-89：回放文件缺失或序列对不上时，报 ReplayMismatch 并指出通道和序号，不改走在线调用（可见组，冻结）。

第二组（10/16–10/18，回放先做）。只用手写的回放文件和替身注册表，不依赖其他线。
「不改走在线调用」从外面看：抛出的就是 ReplayMismatch（conftest 拦住了一切出站连接，若改走在线会变成
NetworkBlocked 或别的错误），回放文件也没有被改写。
"""

import pytest

from e_support import StubRegistry, replay_header
from e_support.replayfiles import (
    ROUND_QIDS,
    jev_state,
    offer_call,
    record_line,
    round_response,
    said_questions,
    write_replay,
)
from waitless import models as m
from waitless.interfaces import ReplayMismatch

ROUND_SHAPE = m.ReplayShape(point="call_round", question_ids=ROUND_QIDS)
SAID_SHAPE = m.ReplayShape(point="said_as_decided", question_ids=["said"])


def _player(path):
    from waitless.replay.player import ReplayPlayer

    return ReplayPlayer(path, registry=StubRegistry())


def _offer_response(tool: str) -> dict:
    return {
        "output": {"message": {"role": "assistant", "content": [{"toolUse": {"toolUseId": "t-1", "name": tool, "input": {"options": []}}}]}},
        "stopReason": "tool_use",
        "usage": {"inputTokens": 0, "outputTokens": 0, "totalTokens": 0},
        "metrics": {"latencyMs": 0},
    }


def test_ac89_missing_replay_file_raises_replay_mismatch(tmp_path):
    missing = tmp_path / "replays" / "baseline.jsonl"
    with pytest.raises(ReplayMismatch) as err:
        player = _player(missing)
        player.next("jev", ROUND_SHAPE)
    assert isinstance(err.value.channel, str) and err.value.channel
    assert isinstance(err.value.seq, int) and err.value.seq >= 0


def test_ac89_exhausted_channel_names_channel_and_next_seq(tmp_path):
    path = write_replay(
        tmp_path / "replays" / "short.jsonl",
        replay_header(),
        [
            record_line("jev", 0, ROUND_SHAPE.model_dump(exclude_none=True), round_response("1")),
            record_line("jev", 1, ROUND_SHAPE.model_dump(exclude_none=True), round_response("1")),
        ],
    )
    player = _player(path)
    player.next("jev", ROUND_SHAPE)
    player.next("jev", ROUND_SHAPE)
    with pytest.raises(ReplayMismatch) as err:
        player.next("jev", ROUND_SHAPE)
    assert (err.value.channel, err.value.seq) == ("jev", 2)

    with pytest.raises(ReplayMismatch) as err2:
        player.next("converse:extract_result_items", m.ReplayShape(task="extract_result_items", tool="extract_result_items_tool"))
    assert (err2.value.channel, err2.value.seq) == ("converse:extract_result_items", 0)


def test_ac89_shape_mismatch_names_channel_and_seq(tmp_path):
    path = write_replay(
        tmp_path / "replays" / "shapes.jsonl",
        replay_header(),
        [
            record_line("jev", 0, ROUND_SHAPE.model_dump(exclude_none=True), round_response("1")),
            record_line(
                "converse:extract_offer_options",
                0,
                {"task": "extract_offer_options", "tool": "extract_offer_options_tool"},
                _offer_response("extract_offer_options_tool"),
            ),
        ],
    )
    player = _player(path)
    with pytest.raises(ReplayMismatch) as err:
        player.next("jev", SAID_SHAPE)
    assert (err.value.channel, err.value.seq) == ("jev", 0)

    with pytest.raises(ReplayMismatch) as err2:
        player.next("converse:extract_offer_options", m.ReplayShape(task="extract_offer_options", tool="some_other_tool"))
    assert (err2.value.channel, err2.value.seq) == ("converse:extract_offer_options", 0)


async def test_ac89_clients_raise_mismatch_instead_of_going_online(tmp_path):
    path = write_replay(
        tmp_path / "replays" / "round-only.jsonl",
        replay_header(),
        [record_line("jev", 0, ROUND_SHAPE.model_dump(exclude_none=True), round_response("1"))],
    )
    before = path.read_text(encoding="utf-8")
    player = _player(path)

    with pytest.raises(ReplayMismatch) as err:
        await player.jev_client().ask(jev_state(), said_questions())
    assert (err.value.channel, err.value.seq) == ("jev", 0)
    with pytest.raises(ReplayMismatch) as err2:
        await player.converse_client().converse(offer_call("extract_offer_options_tool"))
    assert err2.value.channel == "converse:extract_offer_options" and err2.value.seq == 0

    assert path.read_text(encoding="utf-8") == before, "the player must not rewrite or extend the replay file"
