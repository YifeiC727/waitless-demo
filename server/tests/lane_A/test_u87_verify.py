import pytest
from pydantic import ValidationError
from fakes import FakeJev
from waitless.utility_lookup import LiveLookupConfig, LiveLookup, verify_lookup
from .test_u85_verify import TEST_CFG, FICT, NUM, P_CONTACT, P_START, P_GOV, P_DIR, CLAIM, evidence
from .u87_support import SITE, P_HOME, Searcher

def test_u87_1_one_page_on_the_site_is_enough():
    v = verify_lookup(evidence([P_CONTACT, P_DIR]), site=SITE, config=TEST_CFG)
    assert (v.verified, v.reason, v.company_name, v.official_number) == (True, "on_site", FICT, NUM)
    assert [s.url for s in v.sources] == [P_CONTACT.url]
    assert [s.title for s in v.sources] == ["Contact us"]

def test_u87_1_every_page_with_the_number_is_a_source():
    v = verify_lookup(evidence([P_CONTACT, P_START, P_HOME]), site=SITE, config=TEST_CFG)
    assert [s.url for s in v.sources] == [P_CONTACT.url, P_START.url]
    v = verify_lookup(evidence([P_CONTACT, P_START, P_HOME]), site=SITE, config=TEST_CFG.model_copy(update={"max_sources_shown": 1}))
    assert [s.url for s in v.sources] == [P_CONTACT.url]
    v = verify_lookup(evidence([P_CONTACT, P_CONTACT.model_copy(update={"url": P_CONTACT.url + "#top"})]), site=SITE, config=TEST_CFG)
    assert len(v.sources) == 1

def test_u87_1_number_must_be_on_the_chosen_site():
    for pages, site in [([P_DIR], SITE), ([P_GOV], SITE), ([P_HOME], SITE), ([P_CONTACT], "compare-power.test")]:
        v = verify_lookup(evidence(pages), site=site, config=TEST_CFG)
        assert (v.verified, v.reason, v.sources) == (False, "number_not_on_site", [])

def test_u87_1_subdomains_count_lookalikes_do_not():
    for url, reason in [("https://help.prairie-light.example/contact", "on_site"), ("https://prairie-light.example.test/contact", "number_not_on_site"), ("https://notprairie-light.example/contact", "number_not_on_site"), ("ftp://prairie-light.example/contact", "number_not_on_site")]:
        v = verify_lookup(evidence([P_CONTACT.model_copy(update={"url": url})]), site=SITE, config=TEST_CFG)
        assert v.reason == reason

def test_u87_1_claim_checks():
    for claim, error, reason in [(CLAIM.model_copy(update={"found": False}), None, "not_found"), (CLAIM.model_copy(update={"company_name": "  "}), None, "not_found"), (None, None, "no_report"), (CLAIM, "timeout", "timeout"), (CLAIM.model_copy(update={"phone_number": "555-0199"}), None, "bad_number")]:
        v = verify_lookup(evidence([P_CONTACT], claim=claim, error=error), site=SITE, config=TEST_CFG)
        assert (v.verified, v.reason) == (False, reason)

def test_u87_1_claimed_domain_and_name_are_not_checked():
    for domain in ("other.example", "not a domain"):
        v = verify_lookup(evidence([P_CONTACT], claim=CLAIM.model_copy(update={"website_domain": domain})), site=SITE, config=TEST_CFG)
        assert v.reason == "on_site"
    v = verify_lookup(evidence([P_START], claim=CLAIM.model_copy(update={"company_name": "Prairie Power Company"})), site=SITE, config=TEST_CFG)
    assert v.reason == "on_site"

def test_u87_1_config_v2_rules():
    for x in ({"version": 1}, {"extra_official_suffixes": [".gov"]}, {"jev_site_min_confidence": 0}, {"jev_site_min_confidence": 1.5}, {"jev_number_min_probability": 0}, {"jev_site_none": ""}):
        with pytest.raises(ValidationError):
            LiveLookupConfig.model_validate(TEST_CFG.model_dump() | x)
    with pytest.raises(ValueError):
        LiveLookup(searcher=Searcher(), jev=FakeJev(), config=TEST_CFG, timeout_s=0)
    with pytest.raises(ValueError):
        LiveLookup(searcher=Searcher(), jev=None, config=TEST_CFG, timeout_s=40)
