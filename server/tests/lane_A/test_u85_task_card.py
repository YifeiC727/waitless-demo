import json
import pytest
from a_support import AEnv, LAKESHORE_EXTRACTED, LAKESHORE_REQUEST, default_prepare_gate
from waitless.models import SimulatedLine, Source, UpdateTaskFields
from waitless.utility_lookup import CompanyView
from .test_u85_verify import SERVICE, ADDR, FICT, NUM, P_CONTACT, P_START

class FakeLiveLookup:
    def __init__(self, *views):
        self.views = views
        self.calls = []
    async def run(self, service, addresses):
        self.calls.append((service, addresses))
        return self.views[min(len(self.calls)-1, len(self.views)-1)]

def view(verified=True):
    return CompanyView(name=FICT if verified else "Your local electric utility",
        official_number=NUM if verified else "Not confirmed",
        sources=[Source(url=p.url, title=p.title) for p in (P_CONTACT,P_START)] if verified else [],
        simulated_line=SimulatedLine(name="Simulated utility line", notice="NOTICE-OK" if verified else "NOTICE-NO your area"))

@pytest.fixture
def env(tmp_path):
    e = AEnv(tmp_path)
    e.skills["company-lakeshore-energy"].metadata["live-lookup-service"] = SERVICE
    e.store.save_profile({"name": "Alex Example", "phone_number": "(312) 555-0147", "address": ADDR})
    # The existing start-service fixture also requires the simulated account field.
    e.store.save_profile({"account_number": "8800123456"}, company_id="lakeshore-energy")
    yield e
    e.close()

def service(env, lookup=None):
    from waitless.tasks import TaskService
    base = env.task_service(gate=default_prepare_gate())
    return TaskService(store=env.store, trace=env.trace, gate=base.gate, llm=base.llm, skills=env.skills,
                       fetcher=base.fetcher, clock=env.clock, lookup=lookup)

async def prepared(env, lookup=None):
    return await service(env, lookup).prepare(LAKESHORE_REQUEST)

async def test_u85_3_prepare_puts_lookup_on_card(env):
    fake = FakeLiveLookup(view())
    out = await prepared(env, fake)
    c = out.task_card
    assert out.status == "ready" and c.company.company_id == "lakeshore-energy"
    assert c.company.name == FICT and c.company.official_number == NUM
    assert [s.url for s in c.company.sources] == [P_CONTACT.url,P_START.url]
    assert c.simulated_line.notice == "NOTICE-OK"
    assert fake.calls == [(SERVICE, [LAKESHORE_EXTRACTED.service_address, ADDR])]

async def test_u85_3_unverified_card_is_still_ready(env):
    out = await prepared(env, FakeLiveLookup(view(False)))
    assert out.status == "ready" and out.task_card.company.official_number == "Not confirmed"
    assert out.task_card.company.sources == [] and out.task_card.simulated_line.notice.startswith("NOTICE-NO")

async def test_u85_3_without_lookup_card_is_unchanged(env):
    import yaml
    c = (await prepared(env)).task_card
    ivr = yaml.safe_load((env.skills["company-lakeshore-energy"].path / "references/ivr-map.yaml").read_text())
    assert c.simulated_line is None and c.company.name == ivr["company_name"]
    assert c.company.official_number == "(312) 555-0100"
    assert len(c.company.sources) == 2
    del env.skills["company-lakeshore-energy"].metadata["live-lookup-service"]
    fake = FakeLiveLookup(view())
    other = (await prepared(env, fake)).task_card
    assert fake.calls == [] and other.simulated_line is None and other.company == c.company

async def test_u85_3_companies_table_keeps_the_simulated_line(env):
    await prepared(env)
    before = env.store.get_company("lakeshore-energy")
    await prepared(env, FakeLiveLookup(view()))
    after = env.store.get_company("lakeshore-energy")
    assert all(after[k] == before[k] for k in ("name","official_number","line_number"))
    assert "Prairie" not in json.dumps(after) and "8005550199" not in json.dumps(after)

async def test_u85_3_update_retries_an_unconfirmed_lookup(env):
    env.store.save_profile({"address": ""})
    fake = FakeLiveLookup(view(False), view())
    svc = service(env, fake)
    out = await svc.prepare(LAKESHORE_REQUEST)
    up = await svc.update(out.task_id, UpdateTaskFields(address=ADDR))
    assert fake.calls[1] == (SERVICE,[ADDR]) and up.task_card.company.name == FICT and up.task_card.card_version == 2

async def test_u85_3_update_keeps_a_verified_lookup(env):
    fake = FakeLiveLookup(view())
    svc = service(env, fake)
    out = await svc.prepare(LAKESHORE_REQUEST)
    up = await svc.update(out.task_id, UpdateTaskFields(name="Alex Example"))
    assert len(fake.calls) == 1 and up.task_card.company == out.task_card.company
