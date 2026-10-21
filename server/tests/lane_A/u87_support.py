import asyncio
from fakes import FakeJev, jev_choice, jev_noul
from waitless.utility_lookup import SearchResult, SiteResults, LookupPage, LiveLookup
from .test_u85_verify import TEST_CFG, evidence, P_CONTACT, P_DIR

SITE = "prairie-light.example"
R_DIR = SearchResult(url="https://phone-directory.test/prairie", title="Prairie Light & Power phone numbers")
R_CMP = SearchResult(url="https://compare-power.test/springfield-il", title="Compare electricity plans in Springfield, IL")
R_OWN = SearchResult(url="https://www.prairie-light.example/", title="Prairie Light & Power | Home")
R_OWN2 = SearchResult(url="https://prairie-light.example/start-stop-move?src=ad", title="Start, stop or move service")
P_HOME = LookupPage(url="https://www.prairie-light.example/", title="Home", text="Welcome to Prairie Light & Power.")

class Searcher:
    def __init__(self, found=None, result=None, delay=0, error=None):
        self.found = found if found is not None else SiteResults(results=[R_DIR, R_OWN, R_OWN2], error=None, searches=1)
        self.result = result if result is not None else evidence([P_CONTACT, P_DIR])
        self.delay, self.error = delay, error
        self.calls = []

    async def find_sites(self, area, service):
        self.calls.append(("find", area, service))
        if self.delay:
            await asyncio.sleep(self.delay)
        if self.error:
            raise self.error
        return self.found

    async def search(self, area, service, *, site):
        self.calls.append(("search", area, service, site))
        return self.result

def jev_ok(conf=0.93, p=0.91):
    return FakeJev().on("official_site", {"official_site": jev_choice("r2", conf)}).on("number_purpose", {"number_purpose": jev_noul(p)})

def live(searcher, jev, timeout=40, config=TEST_CFG):
    return LiveLookup(searcher=searcher, jev=jev, config=config, timeout_s=timeout)
