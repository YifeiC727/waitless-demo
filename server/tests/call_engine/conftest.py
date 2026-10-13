"""D 线测试的 fixture（第 3 步写；可见组，冻结）。替身与构造函数在 _support.py。"""

from typing import Any

import pytest
from fakes import FakeTrace

from call_engine._support import (
    FakeGate,
    FakeStore,
    make_compiled,
    make_deps,
    make_settings,
    make_spec,
)


@pytest.fixture
def engine(engine_config: Any) -> Any:
    """server/config/engine.yaml（conftest 的 engine_config），数值一律从这里取，不在测试里另写。"""
    return engine_config


@pytest.fixture
def settings(mode_env: Any, engine: Any) -> Any:
    return make_settings(mode_env("live-sim"), engine)


@pytest.fixture
def line_number(fake_carrier: Any) -> str:
    return next(iter(fake_carrier.numbers))


@pytest.fixture
def compiled(test_profile: dict[str, str], line_number: str) -> Any:
    return make_compiled(allowed_fields=test_profile, official_number=line_number)


@pytest.fixture
def spec(compiled: Any, line_number: str) -> Any:
    return make_spec(compiled, to_number=line_number)


@pytest.fixture
def trace(fake_clock: Any) -> FakeTrace:
    """共享的 fakes.FakeTrace（G-G-01）：seq 从 0 起；写入时拒绝不合格的事件（ValueError）。"""
    return FakeTrace(fake_clock)


@pytest.fixture
def gate(trace: FakeTrace) -> FakeGate:
    return FakeGate(trace)


@pytest.fixture
def store(test_profile: dict[str, str]) -> FakeStore:
    return FakeStore(test_profile)


@pytest.fixture
def deps(settings: Any, trace: FakeTrace, fake_clock: Any, gate: FakeGate, store: FakeStore) -> Any:
    return make_deps(settings=settings, trace=trace, clock=fake_clock, gate=gate, store=store)
