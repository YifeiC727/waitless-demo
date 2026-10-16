"""U91 / E118: strategy direct asks Claude once, no web tools, no Jev; shows the claim without sources."""
import pytest
from fakes import FakeJev
from waitless.utility_lookup import LookupClaim, LookupEvidence, AnthropicWebSearcher
from .test_u85_verify import SERVICE, ADDR, TEST_CFG, CLAIM
from .u87_support import Searcher, live

DIRECT = TEST_CFG.model_copy(update={"strategy": "direct"})


class DirectSearcher(Searcher):
    def __init__(self, evidence):
        super().__init__()
        self.direct = evidence

    async def ask_direct(self, area, service):
        self.calls.append(("direct", area, service))
        return self.direct


def ev(claim=CLAIM, error=None):
    return LookupEvidence(claim=claim, pages=[], error=error, searches=0, fetches=0)


async def test_u91_direct_shows_the_claim_without_sources():
    s, j = DirectSearcher(ev()), FakeJev()
    view = await live(s, j, config=DIRECT).run(SERVICE, [ADDR])
    assert view.name == CLAIM.company_name
    assert view.official_number == CLAIM.phone_number
    assert view.sources == []
    assert view.simulated_line.notice == "NOTICE-AI"
    assert [c[0] for c in s.calls] == ["direct"]
    assert j.calls == []


@pytest.mark.parametrize("claim,error", [
    (None, None),
    (LookupClaim(found=False, company_name="", website_domain="", phone_number=""), None),
    (CLAIM.model_copy(update={"phone_number": "555-01"}), None),
    (CLAIM, "api_error"),
])
async def test_u91_direct_bad_answers_are_not_confirmed(claim, error):
    view = await live(DirectSearcher(ev(claim, error)), FakeJev(), config=DIRECT).run(SERVICE, [ADDR])
    assert view.official_number == DIRECT.unconfirmed_number
    assert view.sources == []


async def test_u91_direct_result_is_cached():
    s = DirectSearcher(ev())
    lk = live(s, FakeJev(), config=DIRECT)
    await lk.run(SERVICE, [ADDR])
    await lk.run(SERVICE, [ADDR])
    assert [c[0] for c in s.calls] == ["direct"]


async def test_u91_ask_direct_request_shape():
    sent = []

    class Messages:
        async def create(self, **kw):
            sent.append(kw)
            class Block:
                type, name, input = "tool_use", "report_utility", CLAIM.model_dump()
            class Reply:
                content, stop_reason = [Block()], "tool_use"
            return Reply()

    class Client:
        messages = Messages()

    s = AnthropicWebSearcher(api_key="k", base_url="https://api.example", timeout_s=5, model="m", config=DIRECT, client=Client())
    from waitless.utility_lookup import LookupArea
    out = await s.ask_direct(LookupArea(city="Springfield", state="IL"), SERVICE)
    assert out.claim == CLAIM and out.error is None
    kw = sent[0]
    assert kw["system"] == "DIRECT-ONLY"
    assert [t["name"] for t in kw["tools"]] == ["report_utility"]
    assert kw["tool_choice"] == {"type": "tool", "name": "report_utility"}
    assert kw["messages"] == [{"role": "user", "content": f"{SERVICE} in Springfield, IL, US"}]
