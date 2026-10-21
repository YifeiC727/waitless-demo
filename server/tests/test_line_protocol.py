"""电话线协议（第 3 步可见测试，冻结；tech.v4.md §6.3，AC-61）。

r2 起编解码函数体由第 3 步写进共享的 line_protocol.py（歧义表 A-23），61 条在 I-0 就应全过。
"""

import base64
import json
import re
from typing import Any

import pytest
from pydantic import ValidationError

from waitless.call import line_protocol as lp

SID = "MZ" + "a" * 32
CALL = "CA" + "b" * 32
ULAW_20MS = bytes(range(160))
PAYLOAD = base64.b64encode(ULAW_20MS).decode("ascii")


def connected() -> dict[str, Any]:
    return {"event": "connected", "protocol": "Call", "version": "1.0.0"}


def start(**over: Any) -> dict[str, Any]:
    d = {
        "event": "start",
        "sequenceNumber": "1",
        "streamSid": SID,
        "start": {
            "accountSid": "SIM",
            "callSid": CALL,
            "streamSid": SID,
            "tracks": ["inbound"],
            "customParameters": {"call_id": "call-1", "task_id": "task-1"},
            "mediaFormat": {"encoding": "audio/x-mulaw", "sampleRate": 8000, "channels": 1},
        },
    }
    d.update(over)
    return d


def media_in(**over: Any) -> dict[str, Any]:
    d = {
        "event": "media",
        "sequenceNumber": "2",
        "streamSid": SID,
        "media": {"track": "inbound", "chunk": "1", "timestamp": "0", "payload": PAYLOAD},
    }
    d.update(over)
    return d


def mark_in() -> dict[str, Any]:
    return {"event": "mark", "sequenceNumber": "3", "streamSid": SID, "mark": {"name": "menu-end"}}


def stop() -> dict[str, Any]:
    return {"event": "stop", "sequenceNumber": "4", "streamSid": SID, "stop": {"accountSid": "SIM", "callSid": CALL}}


def media_out() -> dict[str, Any]:
    return {"event": "media", "streamSid": SID, "media": {"payload": PAYLOAD}}


def mark_out() -> dict[str, Any]:
    return {"event": "mark", "streamSid": SID, "mark": {"name": "dtmf-1"}}


def clear() -> dict[str, Any]:
    return {"event": "clear", "streamSid": SID}


TO_ENGINE = [
    ("connected", connected, lp.ConnectedMsg),
    ("start", start, lp.StartMsg),
    ("media", media_in, lp.MediaMsg),
    ("mark", mark_in, lp.MarkMsg),
    ("stop", stop, lp.StopMsg),
]
TO_CARRIER = [
    ("media", media_out, lp.MediaMsg),
    ("mark", mark_out, lp.MarkMsg),
    ("clear", clear, lp.ClearMsg),
]


# ---------------------------------------------------------------------------
# 模型（数据定义，现在即可通过）
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("name, build, model", TO_ENGINE + TO_CARRIER, ids=lambda v: v if isinstance(v, str) else None)
def test_spec_examples_validate(name: str, build: Any, model: Any) -> None:
    model.model_validate(build())


@pytest.mark.parametrize(
    "mutate",
    [
        lambda d: d.update(streamSid="MZ123"),
        lambda d: d.update(streamSid="XX" + "a" * 32),
        lambda d: d["start"].update(callSid="CA" + "z" * 32),
        lambda d: d["start"].update(streamSid="MZ" + "c" * 32),
        lambda d: d["start"]["mediaFormat"].update(sampleRate=16000),
        lambda d: d["start"]["mediaFormat"].update(encoding="audio/l16"),
        lambda d: d["start"]["mediaFormat"].update(channels=2),
        lambda d: d["start"].update(tracks=["outbound"]),
        lambda d: d["start"].update(tracks=[]),
        lambda d: d["start"]["customParameters"].pop("task_id"),
        lambda d: d["start"]["customParameters"].update(call_id=""),
        lambda d: d.update(sequenceNumber="one"),
        lambda d: d.update(extra=True),
    ],
    ids=[
        "short-stream-sid", "stream-sid-prefix", "call-sid-not-hex", "start-stream-sid-mismatch", "sample-rate",
        "encoding", "channels", "track", "no-tracks", "no-task-id", "empty-call-id", "sequence-not-digits",
        "extra-field",
    ],
)
def test_start_model_rejects_bad_fields(mutate: Any) -> None:
    d = start()
    mutate(d)
    with pytest.raises(ValidationError):
        lp.StartMsg.model_validate(d)


def test_media_payload_must_be_base64() -> None:
    with pytest.raises(ValidationError):
        lp.MediaMsg.model_validate(media_in(media={"track": "inbound", "chunk": "1", "timestamp": "0", "payload": "not base64!"}))


def test_media_track_is_inbound_only() -> None:
    with pytest.raises(ValidationError):
        lp.MediaMsg.model_validate(media_in(media={"track": "outbound", "chunk": "1", "timestamp": "0", "payload": PAYLOAD}))


def test_protocol_constants() -> None:
    assert lp.MULAW_FRAME_BYTES == 160 and lp.LINE_FRAME_MS == 20
    assert lp.EVENTS_BY_DIRECTION["to_engine"] == {"connected", "start", "media", "mark", "stop"}
    assert lp.EVENTS_BY_DIRECTION["to_carrier"] == {"media", "mark", "clear"}


# ---------------------------------------------------------------------------
# 解析（第 3 步实现，A-23）
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("name, build, model", TO_ENGINE, ids=[n for n, _, _ in TO_ENGINE])
def test_parse_to_engine_messages(name: str, build: Any, model: Any) -> None:
    msg = lp.parse_line_message(json.dumps(build()), direction="to_engine")
    assert isinstance(msg, model)
    assert msg == model.model_validate(build())


@pytest.mark.parametrize("name, build, model", TO_CARRIER, ids=[n for n, _, _ in TO_CARRIER])
def test_parse_to_carrier_messages(name: str, build: Any, model: Any) -> None:
    msg = lp.parse_line_message(json.dumps(build()), direction="to_carrier")
    assert isinstance(msg, model)
    assert msg == model.model_validate(build())


def test_parse_accepts_bytes_and_mappings() -> None:
    assert isinstance(lp.parse_line_message(json.dumps(start()).encode(), direction="to_engine"), lp.StartMsg)
    assert isinstance(lp.parse_line_message(clear(), direction="to_carrier"), lp.ClearMsg)


@pytest.mark.parametrize(
    "raw, direction",
    [
        (clear(), "to_engine"),
        (connected(), "to_carrier"),
        (start(), "to_carrier"),
        (stop(), "to_carrier"),
    ],
    ids=["clear-to-engine", "connected-to-carrier", "start-to-carrier", "stop-to-carrier"],
)
def test_parse_rejects_events_not_allowed_in_direction(raw: dict[str, Any], direction: str) -> None:
    with pytest.raises(lp.LineProtocolError):
        lp.parse_line_message(json.dumps(raw), direction=direction)


@pytest.mark.parametrize(
    "raw",
    ["not json", "[]", "42", json.dumps({"streamSid": SID}), json.dumps({"event": "dtmf", "streamSid": SID})],
    ids=["not-json", "array", "number", "no-event", "unknown-event"],
)
def test_parse_rejects_malformed_frames(raw: str) -> None:
    with pytest.raises(lp.LineProtocolError):
        lp.parse_line_message(raw, direction="to_engine")


def test_parse_wraps_validation_errors() -> None:
    bad = start()
    bad["start"]["mediaFormat"]["sampleRate"] = 16000
    with pytest.raises(lp.LineProtocolError) as info:
        lp.parse_line_message(json.dumps(bad), direction="to_engine")
    assert not isinstance(info.value, ValidationError)


@pytest.mark.parametrize("field", ["track", "chunk", "timestamp"])
def test_inbound_media_needs_track_chunk_timestamp(field: str) -> None:
    d = media_in()
    del d["media"][field]
    with pytest.raises(lp.LineProtocolError):
        lp.parse_line_message(json.dumps(d), direction="to_engine")


def test_inbound_media_and_mark_need_sequence_number() -> None:
    d = media_in()
    del d["sequenceNumber"]
    with pytest.raises(lp.LineProtocolError):
        lp.parse_line_message(json.dumps(d), direction="to_engine")
    d = mark_in()
    del d["sequenceNumber"]
    with pytest.raises(lp.LineProtocolError):
        lp.parse_line_message(json.dumps(d), direction="to_engine")


# ---------------------------------------------------------------------------
# 构造与编码（第 3 步实现，A-23）
# ---------------------------------------------------------------------------


def test_encoded_shapes_match_section_6_3() -> None:
    assert json.loads(lp.encode_line_message(lp.make_connected())) == connected()
    assert json.loads(lp.encode_line_message(lp.make_media_out(stream_sid=SID, ulaw=ULAW_20MS))) == media_out()
    assert json.loads(lp.encode_line_message(lp.make_clear(stream_sid=SID))) == clear()
    assert json.loads(lp.encode_line_message(lp.make_mark(stream_sid=SID, name="dtmf-1"))) == mark_out()
    assert json.loads(lp.encode_line_message(lp.make_stop(sequence=4, stream_sid=SID, call_sid=CALL))) == stop()
    built = lp.make_start(sequence=1, stream_sid=SID, call_sid=CALL, call_id="call-1", task_id="task-1")
    assert json.loads(lp.encode_line_message(built)) == start()
    media = lp.make_media_in(sequence=2, stream_sid=SID, chunk=1, timestamp_ms=0, ulaw=ULAW_20MS)
    assert json.loads(lp.encode_line_message(media)) == media_in()


def test_encoding_is_compact_json_text() -> None:
    text = lp.encode_line_message(lp.make_media_out(stream_sid=SID, ulaw=ULAW_20MS))
    assert isinstance(text, str)
    assert text == json.dumps(json.loads(text), separators=(",", ":"))


@pytest.mark.parametrize(
    "build, direction",
    [
        (lambda: lp.make_connected(), "to_engine"),
        (lambda: lp.make_start(sequence=1, stream_sid=SID, call_sid=CALL, call_id="c", task_id="t"), "to_engine"),
        (lambda: lp.make_media_in(sequence=2, stream_sid=SID, chunk=1, timestamp_ms=20, ulaw=ULAW_20MS), "to_engine"),
        (lambda: lp.make_mark(stream_sid=SID, name="m", sequence=3), "to_engine"),
        (lambda: lp.make_stop(sequence=9, stream_sid=SID, call_sid=CALL), "to_engine"),
        (lambda: lp.make_media_out(stream_sid=SID, ulaw=ULAW_20MS), "to_carrier"),
        (lambda: lp.make_mark(stream_sid=SID, name="m"), "to_carrier"),
        (lambda: lp.make_clear(stream_sid=SID), "to_carrier"),
    ],
    ids=["connected", "start", "media-in", "mark-in", "stop", "media-out", "mark-out", "clear"],
)
def test_round_trip(build: Any, direction: str) -> None:
    msg = build()
    assert lp.parse_line_message(lp.encode_line_message(msg), direction=direction) == msg


def test_media_payload_round_trip_and_timestamps() -> None:
    msg = lp.make_media_in(sequence=7, stream_sid=SID, chunk=6, timestamp_ms=100, ulaw=ULAW_20MS)
    assert msg.sequenceNumber == "7" and msg.media.chunk == "6" and msg.media.timestamp == "100"
    assert lp.media_payload_bytes(msg) == ULAW_20MS
    assert lp.media_payload_bytes(lp.make_media_out(stream_sid=SID, ulaw=b"\xff" * 80)) == b"\xff" * 80


def test_make_start_fixes_format_and_parameters() -> None:
    msg = lp.make_start(sequence=1, stream_sid=SID, call_sid=CALL, call_id="call-9", task_id="task-9")
    assert msg.start.tracks == ["inbound"]
    assert (msg.start.mediaFormat.encoding, msg.start.mediaFormat.sampleRate, msg.start.mediaFormat.channels) == (
        "audio/x-mulaw", 8000, 1,
    )
    assert msg.start.customParameters.call_id == "call-9" and msg.start.customParameters.task_id == "task-9"
    assert msg.start.accountSid == lp.SIM_ACCOUNT_SID and msg.sequenceNumber == "1"


def test_mark_sequence_only_when_given() -> None:
    assert lp.make_mark(stream_sid=SID, name="x").sequenceNumber is None
    assert lp.make_mark(stream_sid=SID, name="x", sequence=5).sequenceNumber == "5"


def test_new_sids_are_well_formed_and_unique() -> None:
    streams = {lp.new_stream_sid() for _ in range(200)}
    calls = {lp.new_call_sid() for _ in range(200)}
    assert len(streams) == 200 and len(calls) == 200
    assert all(re.fullmatch(lp.STREAM_SID_PATTERN, s) for s in streams)
    assert all(re.fullmatch(lp.CALL_SID_PATTERN, c) for c in calls)
