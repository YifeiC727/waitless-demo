from waitless.utility_lookup import SearchResult, SiteResults, site_candidates
from .test_u85_verify import SERVICE, ADDR, AREA, TEST_CFG
from .u87_support import R_DIR, R_OWN, R_OWN2, Searcher, jev_ok, live


def test_u87b_1_subdomains_of_one_company_are_one_candidate():
    cs = site_candidates([
        R_DIR,
        SearchResult(url="https://secure.prairie-light.example/login", title="Sign in"),
        R_OWN,
        R_OWN2,
    ], limit=20, max_chars=160)
    assert [c.id for c in cs] == ["r1", "r2"]
    assert [c.site for c in cs] == ["phone-directory.test", "prairie-light.example"]
    assert cs[1].url == "https://secure.prairie-light.example/login"
    assert cs[1].description == "Sign in - secure.prairie-light.example/login"


def test_u87b_1_country_code_second_level_keeps_three_labels():
    cs = site_candidates([
        SearchResult(url="https://www.help.prairie-light.co.uk/contact", title="Contact"),
        SearchResult(url="https://shop.prairie-light.com.au/", title="Shop"),
    ], limit=20, max_chars=160)
    assert [c.site for c in cs] == ["prairie-light.co.uk", "prairie-light.com.au"]


async def test_u87b_1_number_search_uses_the_registrable_site():
    searcher = Searcher(found=SiteResults(results=[
        R_DIR,
        SearchResult(url="https://secure.prairie-light.example/login", title="Sign in"),
    ], error=None, searches=1))
    view = await live(searcher, jev_ok()).run(SERVICE, [ADDR])
    assert ("search", AREA, SERVICE, "prairie-light.example") in searcher.calls
    assert view.official_number != TEST_CFG.unconfirmed_number
