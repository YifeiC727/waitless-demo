import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock
from uuid import uuid4
import pytest
from call_engine.conftest import engine, settings, compiled, line_number, spec, trace, gate, store, deps
from waitless.call.live import LiveCall
from waitless.call.media_gate import MediaGate


@pytest.fixture
def call(spec, deps):
    deps.settings.engine.speaker.teardown_step_timeout_ms = 15
    deps.settings.engine.speaker.teardown_cancel_grace_ms = 15
    carrier = SimpleNamespace(hangup=AsyncMock(), aclose=AsyncMock())
    spec = spec.model_copy(update={'call_id': uuid4().hex})
    c = LiveCall(spec, deps, carrier=carrier)
    c._owns_carrier = True
    c._call_sid = 'test-call'
    c._toolbox = SimpleNamespace(handoff_decision_id=None, action_phase=None, phase='hold',
        status_line='hold', interrupts_used=0, close=AsyncMock())
    c._media_gate = MediaGate(dtmf=deps.settings.engine.dtmf, clock=deps.clock,
        output_buffer_ms=deps.settings.engine.speaker.output_buffer_ms)
    c._media_gate.begin_pending(1)
    c._tap = SimpleNamespace(discard_pending=lambda: None, close=AsyncMock())
    c._keypad = SimpleNamespace(close=AsyncMock())
    c._listen_hub = SimpleNamespace(close=AsyncMock())
    c._task = SimpleNamespace(cancel=AsyncMock())
    return c


def trigger(**extra):
    return dict(seq=1, decision_id='source-decision', **extra)


async def turns(n=40):
    for _ in range(n):
        await asyncio.sleep(0)


class Stubborn:
    """A cooperative step which deliberately outlives its cancellation grace."""
    instances = []

    def __init__(self, outcome='done'):
        self.instances.append(self)
        self.started, self.release = asyncio.Event(), asyncio.Event()
        self.outcome = outcome
        self.cancelled = False

    async def __call__(self, *args):
        self.started.set()
        while not self.release.is_set():
            try:
                await self.release.wait()
            except asyncio.CancelledError:
                self.cancelled = True
        if self.outcome == 'error':
            raise LookupError('test failure')
        if self.outcome == 'cancel':
            raise asyncio.CancelledError


@pytest.fixture(autouse=True)
async def release_test_steps():
    Stubborn.instances.clear()
    yield
    for step in Stubborn.instances:
        step.release.set()
    await turns()
    Stubborn.instances.clear()
