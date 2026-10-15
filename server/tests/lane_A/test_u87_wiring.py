import shutil
import anthropic
import pytest
import yaml
from fakes import FakeJev, FakeConverse
from waitless.config import load_settings
from waitless.app import build_services
from .test_u85_verify import TEST_CFG
from .test_u85_lookup_config import LOOKUP

def test_u87_2_build_services_gives_lookup_the_jev_client(mode_env, tmp_path, skills_dir, monkeypatch):
    copied = tmp_path / "skills"
    shutil.copytree(skills_dir, copied)
    (copied / "find-official-number/references/live-lookup.yaml").write_text(yaml.safe_dump(TEST_CFG.model_dump()))
    def no_create(*a, **kw):
        pytest.fail("SDK called during construction")
    monkeypatch.setattr(anthropic.resources.messages.AsyncMessages, "create", no_create)
    fake = FakeJev()
    for on in (True, False):
        settings = load_settings(mode_env("text-sim", WAITLESS_SKILLS_DIR=str(copied), **(LOOKUP if on else {})))
        services, parts = build_services(settings, jev=fake, converse=FakeConverse())
        try:
            if on:
                assert parts.tasks.lookup.jev is fake
                assert not hasattr(parts.tasks.lookup, "min_agreeing_sources")
            else:
                assert parts.tasks.lookup is None
            assert fake.calls == []
        finally:
            services.store.close()
