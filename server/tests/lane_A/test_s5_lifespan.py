import sqlite3
from types import SimpleNamespace

import pytest

from a_support import AEnv
import waitless.app as app_module


@pytest.mark.parametrize("failure", ["body", "shutdown"])
async def test_owned_resources_close_when_lifespan_raises(tmp_path, monkeypatch, failure):
    env = AEnv(tmp_path)
    parts = env.parts()
    closed = []

    class Resource:
        async def aclose(self):
            closed.append("http")

    async def shutdown():
        closed.append("calls")
        if failure == "shutdown":
            raise RuntimeError("shutdown failure")

    monkeypatch.setattr(parts.calls, "shutdown", shutdown)
    services = SimpleNamespace(store=env.store, _resources=[Resource()])
    monkeypatch.setattr(app_module, "build_services", lambda settings: (services, parts))
    app = app_module.create_app(env.settings)
    try:
        with pytest.raises(ExceptionGroup):
            async with app.router.lifespan_context(app):
                if failure == "body":
                    raise RuntimeError("body failure")
        assert closed == ["calls", "http"]
        with pytest.raises(sqlite3.ProgrammingError, match="closed"):
            env.store.get_profile()
    finally:
        env.close()
