"""U112: private DOB speech, then one choice after a fresh long question."""

import asyncio
import json
from dataclasses import replace
from types import SimpleNamespace

import pytest
import yaml
from websockets.exceptions import ConnectionClosedOK

from a_support import canned_outputs, make_settings
from waitless import cli, config
from waitless.models import HANDOFF_FRAME_BYTES, HANDOFF_SAMPLE_RATE
from .h3_support import HOLDER_SAY, two_question_company
from .test_h3a1_holder_config import holder_check
from .test_h3a2_holder_sim import THRESHOLD, TONE
from .test_r9_live_pending import pending_check


@pytest.fixture
async def choice_plan(tmp_path, company_dir, monkeypatch):
    root, path, synth, _ = two_question_company(tmp_path, company_dir, monkeypatch)
    settings = make_settings(tmp_path / "settings", company_dir=root)
    plan = await cli._prepare_holder_plan(yaml.safe_load(path.read_text()), settings)
    synth.assert_called_once()
    return plan


class VirtualHandoff:
    """Drive real reader/sender tasks with a clock advanced only at waits."""

    def __init__(self, monkeypatch, *, bursts=(), during_reply=False, ending=None,
                 ack=False, block_keys=False, send_delay=0):
        self.monkeypatch = monkeypatch
        self.bursts = bursts
        self.during_reply = during_reply
        self.ending = ending
        self.ack = ack
        self.block_keys = block_keys
        self.send_delay = send_delay
        self.now = 0.0
        self.events = [(0.0, TONE)]
        self.sent = []
        self.received = []
        self.tasks = []
        self.timeouts = []
        self.incoming = asyncio.Queue()
        self.recv_finished = False
        self.reply_finished = None

    def schedule(self, at, frame):
        self.events.append((at, frame))
        self.events.sort(key=lambda event: event[0])

    async def recv(self):
        try:
            while True:
                frame = await self.incoming.get()
                if frame == "close":
                    raise ConnectionClosedOK(None, None)
                if frame == "cancel":
                    self.caller.cancel()
                    continue
                self.received.append((self.now, frame))
                return frame
        finally:
            self.recv_finished = True

    async def send(self, frame):
        if isinstance(frame, str) and self.block_keys:
            await asyncio.get_running_loop().create_future()
        if isinstance(frame, bytes) and self.send_delay:
            ready = asyncio.get_running_loop().create_future()
            self.schedule(self.now + self.send_delay, ready)
            await ready
        self.sent.append((self.now, frame))
        if isinstance(frame, bytes):
            count = sum(isinstance(value, bytes) for _, value in self.sent)
            if count == 1 and self.during_reply:
                for i in range(1, len(self.plan.frames)):
                    self.schedule(self.now + i * self.frame_seconds / 2, TONE)
            if count == len(self.plan.frames):
                self.reply_finished = self.now
                for offset, duration in self.bursts:
                    for i in range(round(duration / self.frame_seconds) + 1):
                        self.schedule(self.now + offset + i * self.frame_seconds, TONE)
                if self.ending:
                    offset, kind = self.ending
                    frame = json.dumps({"type": "ended"}) if kind == "ended" else kind
                    self.schedule(self.now + offset, frame)
        elif self.ack:
            self.schedule(self.now + self.frame_seconds,
                          json.dumps({"type": "dtmf_ack", "count": 1}))

    async def run(self, plan):
        self.plan = plan
        self.frame_seconds = HANDOFF_FRAME_BYTES / 2 / HANDOFF_SAMPLE_RATE
        self.caller = asyncio.current_task()
        loop = asyncio.get_running_loop()
        real_wait, real_sleep = asyncio.wait, asyncio.sleep
        real_task, real_timeout = asyncio.create_task, asyncio.timeout_at

        async def settle():
            for _ in range(4):
                await real_sleep(0)

        async def virtual_wait(tasks, *, timeout=None, return_when=asyncio.ALL_COMPLETED):
            tasks = set(tasks)
            deadline = None if timeout is None else self.now + max(0, timeout)
            while True:
                while self.events and self.events[0][0] <= self.now + 1e-10:
                    _, frame = self.events.pop(0)
                    if asyncio.isfuture(frame):
                        if not frame.done():
                            frame.set_result(None)
                    else:
                        self.incoming.put_nowait(frame)
                await settle()
                done = {task for task in tasks if task.done()}
                if len(done) == len(tasks) or (done and return_when == asyncio.FIRST_COMPLETED):
                    return done, tasks - done
                if deadline is not None and self.now >= deadline:
                    return done, tasks - done
                candidates = [at for at in (deadline, self.events[0][0] if self.events else None)
                              if at is not None]
                if deadline is None:
                    candidates += [bound.when() + 1e-9 for bound in self.timeouts
                                   if bound.when() is not None and not bound.expired()]
                if not candidates:
                    return await real_wait(tasks, return_when=return_when)
                self.now = max(self.now, min(candidates))

        def create_task(coro, **kwargs):
            task = real_task(coro, **kwargs)
            self.tasks.append(task)
            return task

        def timeout_at(deadline):
            bound = real_timeout(deadline)
            self.timeouts.append(bound)
            return bound

        try:
            with self.monkeypatch.context() as patches:
                patches.setattr(loop, "time", lambda: self.now)
                patches.setattr(asyncio, "wait", virtual_wait)
                patches.setattr(asyncio, "create_task", create_task)
                patches.setattr(asyncio, "timeout_at", timeout_at)
                return await cli._simulate_account_holder(
                    self, plan, threshold_dbfs=THRESHOLD,
                    question_end_silence_ms=200, frame_ms=20,
                )
        finally:
            assert self.recv_finished
            assert self.tasks and all(task.done() for task in self.tasks)

    @property
    def keys(self):
        return [(at, json.loads(frame)) for at, frame in self.sent if isinstance(frame, str)]


def test_u112_a_1_config_then_keys(holder_check, tmp_path, company_dir, monkeypatch, capsys):
    check = holder_check
    _, path, synth, polly = two_question_company(tmp_path / "two", company_dir, monkeypatch)
    raw = yaml.safe_load(path.read_text())
    reply = raw["steps"]["rep"]["scripted"]["holder_reply"]
    for value in ("1", "2", "0123456789*#", "1" * 16):
        reply["then_keys"] = value
        synth.reset_mock()
        plan = asyncio.run(cli._prepare_holder_plan(raw, check.settings))
        assert plan.then_keys == value
        assert plan.choice_min_voice_ms == raw["steps"]["rep"]["scripted"]["holder_prompt_ms"]
        synth.assert_called_once_with(HOLDER_SAY, voice="Salli", polly_client=polly,
                                      sample_rate=HANDOFF_SAMPLE_RATE)
    for value in ("a", "", "1" * 17, None, 1, True, [], {}, "1\n"):
        reply["then_keys"] = value
        synth.reset_mock()
        with pytest.raises(config.ConfigError) as caught:
            asyncio.run(cli._prepare_holder_plan(raw, check.settings))
        assert caught.value.missing == ("steps.rep.scripted.holder_reply.then_keys",)
        assert str(caught.value) == "missing required configuration: steps.rep.scripted.holder_reply.then_keys"
        synth.assert_not_called()
    check.path.write_text(yaml.safe_dump(raw))
    assert cli.main(["check", "live", "--scenario", "baseline"]) == cli.EXIT_CONFIG
    assert capsys.readouterr().err.endswith("steps.rep.scripted.holder_reply.then_keys\n")
    assert "start_assisted_call" not in [name for name, _ in check.handlers.calls]
    check.carrier.set_scenario.assert_not_awaited()
    synth.assert_not_called()
    del reply["then_keys"]
    synth.reset_mock()
    legacy = asyncio.run(cli._prepare_holder_plan(raw, check.settings))
    assert legacy.then_keys is None
    assert legacy.frames == cli._holder_audio_frames(synth.return_value)
    scripted = raw["steps"]["rep"]["scripted"]
    assert legacy.question_wait_ms == (scripted["holder_prompt_ms"] * scripted["holder_max_asks"]
                                       + check.settings.engine.turns.turn_end_silence_ms)
    assert legacy.return_after_ms == scripted["reply_end_silence_ms"]
    synth.assert_called_once()


async def test_u112_a_2_keys_after_second_long_question(choice_plan, monkeypatch):
    # 1.3 s thanks, 1 s gap, then an 8 s choice question.
    for key in ("1", "2"):
        plan = replace(choice_plan, then_keys=key)
        ws = VirtualHandoff(monkeypatch, bursts=((0.02, 1.3), (2.32, 8.0)), send_delay=0.025)
        if key == "2":
            ws.events = [(plan.question_wait_ms / 1000 - 0.24, TONE)]
        assert await ws.run(plan) == "answered"
        assert len(ws.keys) == 1
        at, message = ws.keys[0]
        assert message == {"type": "dtmf", "keys": key}
        assert at >= ws.reply_finished + 10.32 + 0.2 - 1e-9
        assert ws.now == pytest.approx(at + plan.return_after_ms / 1000, abs=1e-9)
        assert len([frame for _, frame in ws.sent if isinstance(frame, bytes)]) == len(plan.frames)


async def test_u112_a_3_old_voice_does_not_trigger(choice_plan, monkeypatch, capsys):
    for bursts in ((), ((0.02, 1.3), (1.72, 1.3), (3.42, 1.3))):
        ws = VirtualHandoff(monkeypatch, bursts=bursts, during_reply=True)
        assert await ws.run(choice_plan) == "no_choice_question"
        assert not ws.keys
        assert ws.now <= ws.reply_finished + choice_plan.question_wait_ms / 1000 + 0.02
        assert capsys.readouterr().err == "check live: holder choice question not heard\n"


def test_u112_a_4_no_second_question_is_bounded(holder_check, monkeypatch, capsys):
    async def missing_question():
        # A short acknowledgement is insufficient even after its silence.
        for bursts in ((), ((0.02, 1.3),), ((0.02, 20.0),)):
            plan = SimpleNamespace(frames=[TONE] * 5, question_wait_ms=6000,
                                   return_after_ms=300, then_keys="9*#", choice_min_voice_ms=3000)
            ws = VirtualHandoff(monkeypatch, bursts=bursts)
            assert await ws.run(plan) == "no_choice_question"
            assert not ws.keys
            assert ws.now == pytest.approx(ws.reply_finished + 6.0, abs=0.02)
            assert capsys.readouterr().err == "check live: holder choice question not heard\n"

    asyncio.run(missing_question())
    # The caller must fail a check even if the result service later says confirmed.
    import websockets

    check = holder_check
    outputs = canned_outputs()
    ready = outputs["get_call_status"].model_copy(update={"ended": False, "pending_choice": None})
    statuses = iter([ready, ready.model_copy(update={"ended": True, "handoff": None})])

    async def status(**kwargs):
        return next(statuses)

    class Connection:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *args):
            return False

        async def send(self, frame):
            pass

        async def recv(self):
            return json.dumps({"type": "ready"})

    async def simulate(*args, **kwargs):
        return "no_choice_question"

    monkeypatch.setattr(check.handlers, "get_call_status", status)
    monkeypatch.setattr(websockets, "connect", lambda url: Connection())
    monkeypatch.setattr(cli, "_simulate_account_holder", simulate)
    check.handlers.outputs["get_case_result"] = outputs["get_case_result"]
    assert cli.main(["check", "live", "--scenario", "baseline"]) == cli.EXIT_CHECK_FAILED
    names = [name for name, _ in check.handlers.calls]
    assert names.count("return_to_agent") == 1
    assert check.order == ["hangup", "server_exit", "company_terminate", "store_close"]


async def test_u112_a_5_repeated_question_sends_once(choice_plan, monkeypatch):
    ws = VirtualHandoff(monkeypatch, bursts=((0.02, 3.2), (3.62, 3.2)))
    assert await ws.run(choice_plan) == "answered"
    assert len(ws.keys) == 1
    assert ws.keys[0][1] == {"type": "dtmf", "keys": "1"}
    assert any(at > ws.keys[0][0] and frame == TONE for at, frame in ws.received)


async def test_u112_a_6_ended_or_closed_mid_stage_two(choice_plan, monkeypatch):
    plan = replace(choice_plan, return_after_ms=300)
    for ending in ("ended", "close", "cancel"):
        ws = VirtualHandoff(monkeypatch, ending=(1.0, ending))
        if ending == "cancel":
            with pytest.raises(asyncio.CancelledError):
                await ws.run(plan)
        else:
            assert await ws.run(plan) == "ended"
        assert not ws.keys
        assert ws.now == pytest.approx(ws.reply_finished + 1.0)
    # A peer end/cancellation must interrupt a DTMF write under backpressure too.
    for ending in ("ended", "close", "cancel", None):
        ws = VirtualHandoff(monkeypatch, bursts=((0.02, 3.2),), block_keys=True,
                            ending=(4.0, ending) if ending else None)
        if ending == "cancel":
            with pytest.raises(asyncio.CancelledError):
                await ws.run(plan)
        elif ending is None:
            with pytest.raises(TimeoutError):
                await ws.run(plan)
        else:
            assert await ws.run(plan) == "ended"
        assert not ws.keys


async def test_u112_a_7_ack_is_not_company_reply(choice_plan, monkeypatch):
    ws = VirtualHandoff(monkeypatch, bursts=((0.02, 3.2),), ack=True,
                        ending=(0.1, json.dumps({"type": "dtmf_ack", "count": 1})))
    assert await ws.run(choice_plan) == "answered"
    assert len(ws.keys) == 1
    assert any(isinstance(frame, str) and json.loads(frame).get("type") == "dtmf_ack"
               for _, frame in ws.received)
    assert ws.now == pytest.approx(ws.keys[0][0] + choice_plan.return_after_ms / 1000, abs=1e-9)


async def test_u112_a_8_legacy_unchanged(monkeypatch):
    plan = SimpleNamespace(frames=[TONE] * 175, question_wait_ms=2000, return_after_ms=300)
    ws = VirtualHandoff(monkeypatch)
    assert await ws.run(plan) == "answered"
    assert [frame for _, frame in ws.sent] == plan.frames
    for i, (at, _) in enumerate(ws.sent):
        assert at == pytest.approx(ws.sent[0][0] + i * ws.frame_seconds, abs=1e-9)
    assert ws.now == pytest.approx(ws.sent[-1][0] + 0.3, abs=1e-9)
    assert not ws.keys
