import asyncio
import json
import logging
import re
from fakes import FakeJev, jev_choice, jev_noul
from waitless.decision.jev import JevError
from waitless.models import SimulatedLine
from waitless.utility_lookup import SiteResults, LookupArea
from .test_u85_verify import SERVICE, ADDR, AREA, FICT, NUM, P_CONTACT, P_DIR, evidence, TEST_CFG
from .test_u85_anthropic_searcher import Client, message, report, searcher
from .test_u87_searcher import results_block
from .u87_support import SITE, R_OWN, Searcher, jev_ok, live

async def test_u87_1_verified_flow():
    s, j = Searcher(), jev_ok()
    v = await live(s, j).run(SERVICE, [None, ADDR])
    assert (v.name, v.official_number) == (FICT, NUM)
    assert [p.url for p in v.sources] == [P_CONTACT.url]
    assert v.simulated_line == SimulatedLine(name="Simulated utility line", notice="NOTICE-OK")
    assert s.calls == [("find", AREA, SERVICE), ("search", AREA, SERVICE, "prairie-light.example")]
    assert [set(c.questions) for c in j.calls] == [{"official_site"}, {"number_purpose"}]

async def test_u87_1_site_question_shape():
    j = jev_ok()
    await live(Searcher(), j).run(SERVICE, [ADDR])
    c = j.calls_for("official_site")[0]
    q = c.questions["official_site"]
    assert q.type == "choice" and q.instructions == "SITE electric utility"
    assert q.criteria == {"r1": "Prairie Light & Power phone numbers - phone-directory.test/prairie", "r2": "Prairie Light & Power | Home - www.prairie-light.example/", "none_of_the_above": "NONE-OF-THESE"}
    assert c.state.task_goal == "GOAL electric utility | Springfield | IL"
    assert c.state.user_profile == {} and c.state.standing_rules == [] and c.state.prior_choices == [] and c.state.transcript.lines == []

async def test_u87_1_number_question_shape():
    j = jev_ok()
    await live(Searcher(), j).run(SERVICE, [ADDR])
    c = j.calls_for("number_purpose")[0]
    q = c.questions["number_purpose"]
    assert q.type == "noul" and q.instructions == "NUMBER electric utility" and q.criteria is None
    lines = c.state.transcript.lines
    assert [l.speaker for l in lines] == ["company", "waitless"]
    assert lines[0].text.startswith("www.prairie-light.example/contact: ") and "1-800-555-0199" in lines[0].text
    assert lines[-1].text == "Lookup claim: company Prairie Light & Power; phone number 1-800-555-0199; official website prairie-light.example"
    assert "phone-directory" not in c.state.model_dump_json()
    assert c.state.user_profile == {}

async def test_u87_1_snippet_window_is_bounded():
    p = P_CONTACT.model_copy(update={"text": "a" * 1000 + " call 1-800-555-0199 " + "b" * 1000})
    j = jev_ok()
    await live(Searcher(result=evidence([p])), j, config=TEST_CFG.model_copy(update={"max_snippet_chars": 100})).run(SERVICE, [ADDR])
    lines = j.calls_for("number_purpose")[0].state.transcript.lines
    snippet = lines[0].text.split(": ", 1)[1]
    assert len(snippet) <= 100 and "1-800-555-0199" in snippet

async def test_u87_1_at_most_seven_page_lines():
    pages = [P_CONTACT.model_copy(update={"url": f"https://prairie-light.example/p{i}"}) for i in range(1, 10)]
    j = jev_ok()
    await live(Searcher(result=evidence(pages)), j).run(SERVICE, [ADDR])
    lines = j.calls_for("number_purpose")[0].state.transcript.lines
    assert len(lines) == 8 and lines[-1].speaker == "waitless"

async def test_u87_1_none_of_the_above_stops():
    s = Searcher()
    j = FakeJev().on("official_site", {"official_site": jev_choice("none_of_the_above", 0.9)})
    v = await live(s, j).run(SERVICE, [ADDR])
    assert v.official_number == "Not confirmed" and v.sources == []
    assert s.calls == [("find", AREA, SERVICE)] and len(j.calls) == 1

async def test_u87_1_low_site_confidence_stops():
    s = Searcher()
    j = FakeJev().on("official_site", {"official_site": jev_choice("r2", 0.6)})
    v = await live(s, j).run(SERVICE, [ADDR])
    assert v.official_number == "Not confirmed" and v.sources == []
    assert s.calls == [("find", AREA, SERVICE)] and len(j.calls) == 1

async def test_u87_1_number_not_on_site_does_not_ask_jev():
    j = jev_ok()
    v = await live(Searcher(result=evidence([P_DIR])), j).run(SERVICE, [ADDR])
    assert v.official_number == "Not confirmed" and j.calls_for("number_purpose") == []

async def test_u87_1_jev_rejects_number():
    v = await live(Searcher(), jev_ok(p=0.4)).run(SERVICE, [ADDR])
    assert v.official_number == "Not confirmed" and v.sources == [] and "Prairie" not in v.model_dump_json()

async def test_u87_1_jev_errors_are_unverified():
    for j in [FakeJev().on("official_site", JevError("x")), FakeJev().on("official_site", {"official_site": jev_choice("r2", 0.93)}).on("number_purpose", JevError("x"))]:
        v = await live(Searcher(), j).run(SERVICE, [ADDR])
        assert v.official_number == "Not confirmed"

async def test_u87_1_no_results_and_search_errors():
    for found in [SiteResults(results=[], error=None, searches=1), SiteResults(results=[], error="refusal", searches=0)]:
        j = jev_ok()
        v = await live(Searcher(found=found), j).run(SERVICE, [ADDR])
        assert v.official_number == "Not confirmed" and j.calls == []

async def test_u87_1_no_area_skips_everything():
    s, j = Searcher(), jev_ok()
    v = await live(s, j).run(SERVICE, ["my new apartment", None])
    assert s.calls == [] and j.calls == [] and v.simulated_line.notice == "NOTICE-NO your area"

async def test_u87_1_first_parseable_address_wins():
    s = Searcher()
    await live(s, jev_ok()).run(SERVICE, ["the new apartment", "10 Oak Ave, Lake View, IL", ADDR])
    assert s.calls[0] == ("find", LookupArea(city="Lake View", state="IL"), SERVICE)

async def test_u87_1_timeout_covers_jev():
    class SlowJev:
        async def ask(self, state, questions):
            await asyncio.sleep(1)
    v = await live(Searcher(), SlowJev(), timeout=0.05).run(SERVICE, [ADDR])
    assert v.official_number == "Not confirmed"

async def test_u87_1_searcher_exception_is_unverified():
    v = await live(Searcher(error=RuntimeError("test")), jev_ok()).run(SERVICE, [ADDR])
    assert v.official_number == "Not confirmed"

async def test_u87_1_cache_keeps_only_verified():
    s, j = Searcher(), jev_ok()
    lookup = live(s, j)
    await lookup.run(SERVICE, [ADDR])
    await lookup.run(SERVICE, [ADDR])
    assert [c[0] for c in s.calls].count("find") == 1 and len(j.calls) == 2
    a, b = {"official_site": jev_choice("r2", 0.93)}, {"number_purpose": jev_noul(0.4)}
    s, j = Searcher(), FakeJev().on("official_site", a, a).on("number_purpose", b, b)
    lookup = live(s, j)
    await lookup.run(SERVICE, [ADDR])
    await lookup.run(SERVICE, [ADDR])
    assert [c[0] for c in s.calls].count("find") == 2 and len(j.calls) == 4

async def test_u87_1_only_city_state_and_site_leave():
    m1 = message([results_block(R_OWN)], "end_turn")
    m2 = message([{"type": "text", "text": "Call 1-800-555-0199.", "citations": [{"type": "web_search_result_location", "url": P_CONTACT.url, "title": "Contact us", "cited_text": P_CONTACT.text, "encrypted_index": "x"}]}, report()])
    c = Client(m1, m2)
    j = FakeJev().on("official_site", {"official_site": jev_choice("r1", 0.93)}).on("number_purpose", {"number_purpose": jev_noul(0.91)})
    v = await live(searcher(c), j).run(SERVICE, [ADDR])
    assert v.official_number == NUM
    assert len(c.calls) == 2 and len(j.calls) == 2
    sent = [json.dumps(kw["messages"], default=str) + json.dumps(kw["tools"]) + kw["system"] for kw in c.calls]
    sent += [call.state.model_dump_json() for call in j.calls]
    for payload in sent:
        for value in ("1200", "Example St", "Apt", "62701"):
            assert value not in payload

async def test_u87_1_log_line_has_codes_only(caplog):
    caplog.set_level(logging.INFO, logger="waitless.utility_lookup")
    await live(Searcher(), jev_ok()).run(SERVICE, [ADDR])
    [rec] = caplog.records
    assert re.fullmatch(r"lookup status=verified reason=verified searches=\d+ fetches=\d+ candidates=2 site_confidence=0\.93 number_probability=0\.91 elapsed_ms=\d+", rec.getMessage())
    j = FakeJev().on("official_site", {"official_site": jev_choice("none_of_the_above", 0.9)})
    await live(Searcher(), j).run(SERVICE, [ADDR])
    rec = caplog.records[-1]
    assert "status=unverified reason=no_official_site" in rec.getMessage()
    assert "site_confidence=0.90" in rec.getMessage() and "number_probability=na" in rec.getMessage()
    for value in ("Prairie", "prairie-light", "1-800-555-0199", "8005550199", "Springfield"):
        assert value not in caplog.text
