"""U87d / E117: retry the on-site number search when the number is not on the site."""
from waitless.utility_lookup import LookupEvidence
from .test_u85_verify import SERVICE, ADDR, TEST_CFG, evidence, P_CONTACT, P_DIR
from .u87_support import Searcher, jev_ok, live


class SeqSearcher(Searcher):
    def __init__(self, results):
        super().__init__()
        self.results = list(results)

    async def search(self, area, service, *, site):
        self.calls.append(("search", area, service, site))
        return self.results.pop(0) if len(self.results) > 1 else self.results[0]


def searches(s):
    return [c for c in s.calls if c[0] == "search"]


async def test_u87d_second_try_finds_the_number():
    s = SeqSearcher([evidence([P_DIR]), evidence([P_CONTACT, P_DIR])])
    view = await live(s, jev_ok()).run(SERVICE, [ADDR])
    assert view.official_number != TEST_CFG.unconfirmed_number
    assert len(searches(s)) == 2


async def test_u87d_stops_after_max_attempts():
    s = SeqSearcher([evidence([P_DIR])])
    view = await live(s, jev_ok()).run(SERVICE, [ADDR])
    assert view.official_number == TEST_CFG.unconfirmed_number
    assert len(searches(s)) == TEST_CFG.max_number_attempts == 2


async def test_u87d_other_reasons_do_not_retry():
    s = SeqSearcher([LookupEvidence(claim=None, pages=[], error="api_error", searches=0, fetches=0)])
    view = await live(s, jev_ok()).run(SERVICE, [ADDR])
    assert view.official_number == TEST_CFG.unconfirmed_number
    assert len(searches(s)) == 1
