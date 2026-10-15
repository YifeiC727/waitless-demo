import asyncio
import json
from waitless.utility_lookup import LiveLookup, LookupArea
from waitless.models import SimulatedLine
from .test_u85_verify import SERVICE, ADDR, AREA, FICT, NUM, P_CONTACT, P_START, TEST_CFG, evidence
from .test_u85_anthropic_searcher import Client, message, report, searcher

class Searcher:
    def __init__(self, e=None, *, delay=0, error=None):
        self.e = e if e is not None else evidence([P_CONTACT, P_START])
        self.calls = []
        self.delay = delay
        self.error = error
    async def search(self, area, service):
        self.calls.append((area, service))
        if self.delay:
            await asyncio.sleep(self.delay)
        if self.error:
            raise self.error
        return self.e

def live(s, timeout=40):
    return LiveLookup(searcher=s, config=TEST_CFG, min_agreeing_sources=2, timeout_s=timeout)

async def test_u85_2_verified_view():
    s = Searcher()
    v = await live(s).run(SERVICE, [None, ADDR])
    assert (v.name, v.official_number) == (FICT, NUM)
    assert [p.url for p in v.sources] == [P_CONTACT.url, P_START.url]
    assert v.simulated_line == SimulatedLine(name="Simulated utility line", notice="NOTICE-OK")
    assert s.calls == [(AREA, SERVICE)]

async def test_u85_2_only_city_and_state_reach_the_request():
    c = Client(message([report()]))
    await live(searcher(c)).run(SERVICE, [ADDR])
    kw = c.calls[0]
    payload = json.dumps(kw["messages"]) + json.dumps(kw["tools"]) + kw["system"]
    assert all(x not in payload for x in ("1200", "Example St", "Apt", "62701"))

async def test_u85_2_first_parseable_address_wins():
    s = Searcher()
    await live(s).run(SERVICE, ["the new apartment", "10 Oak Ave, Lake View, IL", ADDR])
    assert s.calls == [(LookupArea(city="Lake View", state="IL"), SERVICE)]

async def test_u85_2_unverified_view_names_no_company():
    v = await live(Searcher(evidence([P_CONTACT]))).run(SERVICE, [ADDR])
    assert v.name == "Your local electric utility" and v.official_number == "Not confirmed" and v.sources == []
    assert v.simulated_line.notice == "NOTICE-NO Springfield, IL" and "Prairie" not in v.model_dump_json()

async def test_u85_2_no_area_skips_search():
    s = Searcher()
    v = await live(s).run(SERVICE, ["my new apartment", None])
    assert s.calls == [] and v.simulated_line.notice == "NOTICE-NO your area"

async def test_u85_2_timeout_is_unverified():
    v = await live(Searcher(delay=1), timeout=.01).run(SERVICE, [ADDR])
    assert v.official_number == "Not confirmed"

async def test_u85_2_searcher_exception_is_unverified():
    v = await live(Searcher(error=RuntimeError("test"))).run(SERVICE, [ADDR])
    assert v.official_number == "Not confirmed"

async def test_u85_2_cache_keeps_only_verified():
    for e,count in [(evidence([P_CONTACT,P_START]),1), (evidence([P_CONTACT]),2)]:
        s = Searcher(e)
        lookup = live(s)
        await lookup.run(SERVICE,[ADDR])
        await lookup.run(SERVICE,[ADDR])
        assert len(s.calls) == count
