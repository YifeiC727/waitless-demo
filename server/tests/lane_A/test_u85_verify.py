from waitless.utility_lookup import LiveLookupConfig, LookupArea, LookupPage, LookupClaim, LookupEvidence, verify_lookup
from waitless.official_number import phone_numbers_in
SERVICE = "electric utility"
ADDR = "1200 W Example St, Apt 4B, Springfield, IL 62701"
AREA = LookupArea(city="Springfield", state="IL")
FICT = "Prairie Light & Power"
NUM = "1-800-555-0199"
P_CONTACT = LookupPage(url="https://www.prairie-light.example/contact", title="Contact us", text="Prairie Light & Power customer service: 1-800-555-0199. Outages: (312) 555-0177.")
P_START = LookupPage(url="https://prairie-light.example/start-stop-move", title="Start, stop or move service", text="To start service call 1-800-555-0199.")
P_GOV = LookupPage(url="https://commission.state.test/utilities", title="Utility contacts", text="Prairie Light & Power 800-555-0199")
P_DIR = LookupPage(url="https://phone-directory.test/prairie", title="Directory", text="Prairie Light & Power 1-800-555-0123")
CLAIM = LookupClaim(found=True, company_name=FICT, website_domain="prairie-light.example", phone_number="+1 (800) 555-0199")
TEST_CFG = LiveLookupConfig(version=2, search_tool="web_search_20250305", fetch_tool="web_fetch_20250910",
    site_max_searches=2, max_site_candidates=20, max_option_chars=160,
    max_searches=2, max_fetches=2, max_content_tokens=8000, max_continuations=3, max_tokens=4096,
    effort="medium", max_sources_shown=3, max_snippet_chars=400,
    jev_site_min_confidence=0.8, jev_number_min_probability=0.8,
    simulated_line_name="Simulated utility line", unconfirmed_name="Your local {service}",
    unconfirmed_number="Not confirmed", notice_verified="NOTICE-OK", notice_unverified="NOTICE-NO {area}",
    site_instructions="SEARCH-ONLY", instructions="ON-SITE", jev_task_goal="GOAL {service} | {city} | {state}",
    jev_site_instructions="SITE {service}", jev_site_none="NONE-OF-THESE", jev_number_instructions="NUMBER {service}")

def evidence(pages=(), claim=CLAIM, error=None):
    return LookupEvidence(claim=claim, pages=list(pages), error=error, searches=1, fetches=2)

def test_u85_2_phone_numbers_in_matches_find_official_number():
    assert phone_numbers_in(P_CONTACT.text) == {"8005550199": NUM, "3125550177": "(312) 555-0177"}
