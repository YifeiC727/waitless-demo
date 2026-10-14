"""H3A-R3: diagnose pre-ready closes while keeping the polling flow."""

import json

import pytest
import websockets
from websockets.exceptions import ConnectionClosedError
from websockets.frames import Close

from a_support import canned_outputs
from waitless import cli
from .test_h3a1_holder_config import holder_check
from .test_r9_live_pending import pending_check


@pytest.mark.parametrize("stage,code", [
    ("before_ready", 4401), ("before_ready", 1002), ("before_ready", None),
    ("reader", 4401), ("exit", 4401),
])
async def test_h3ar_only_pre_ready_close_is_diagnosed(holder_check, monkeypatch, capsys, stage, code):
    check = holder_check
    outputs = canned_outputs()
    ready = outputs["get_call_status"].model_copy(update={"ended": False, "pending_choice": None})
    ended = ready.model_copy(update={"ended": True, "handoff": None})
    statuses = iter([ready, ended])

    async def status(**kwargs):
        check.handlers.calls.append(("get_call_status", kwargs))
        return next(statuses)

    check.handlers.get_call_status = status
    check.handlers.outputs["get_case_result"] = outputs["get_case_result"]
    close = ConnectionClosedError(Close(code, "rejection-details") if code is not None else None, None)

    class WS:
        received_ready = False

        async def send(self, frame):
            assert json.loads(frame)["type"] == "hello"

        async def recv(self):
            if stage == "before_ready":
                raise close
            if not self.received_ready:
                self.received_ready = True
                return json.dumps({"type": "ready"})
            if stage == "reader":
                raise close
            return json.dumps({"type": "ended"})

    class Connection:
        async def __aenter__(self):
            return WS()

        async def __aexit__(self, *args):
            if stage == "exit":
                raise close
            return False

    monkeypatch.setattr(websockets, "connect", lambda url: Connection())
    report = await cli.check_live("baseline", check.settings)
    assert report.outcome == "confirmed"
    assert [name for name, _ in check.handlers.calls] == [
        "prepare_task", "update_task", "start_assisted_call", "get_call_status", "join_call",
        "get_call_status", "get_case_result",
    ]
    err = capsys.readouterr().err
    if stage == "before_ready":
        assert err.splitlines() == [f"check live: handoff closed before ready (code={code if code is not None else 'none'})"]
    else:
        assert "handoff closed before ready" not in err
    assert "rejection-details" not in err
