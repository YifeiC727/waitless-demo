import anthropic
import httpx
import pytest
from waitless.utility_lookup import AnthropicWebSearcher
from .test_u85_verify import AREA, SERVICE, CLAIM, P_CONTACT, P_START, TEST_CFG

class Client:
    def __init__(self, *replies):
        self.replies = list(replies)
        self.calls = []
        self.messages = self
    async def create(self, **kw):
        import copy
        self.calls.append(copy.deepcopy(kw))
        reply = self.replies[min(len(self.calls)-1, len(self.replies)-1)]
        if isinstance(reply, Exception):
            raise reply
        return reply

def message(content=(), stop="tool_use"):
    return anthropic.types.Message.model_validate(dict(id="msg_test", type="message", role="assistant",
        model="test-lookup-model", content=list(content), stop_reason=stop, stop_sequence=None,
        usage=dict(input_tokens=1, output_tokens=1)))

def report():
    return dict(type="tool_use", id="tool_report", name="report_utility", input=CLAIM.model_dump())

def searcher(client=None, **kw):
    return AnthropicWebSearcher(**(dict(api_key="test-key", base_url="https://lookup.example", timeout_s=40,
        model="test-lookup-model", config=TEST_CFG, client=client) | kw))

def search_block():
    return dict(type="web_search_tool_result", tool_use_id="srv_search", content=[dict(type="web_search_result",
        url=p.url, title=p.title, encrypted_content="x") for p in (P_CONTACT, P_START)])

def fetch_block(p):
    return dict(type="web_fetch_tool_result", tool_use_id="srv_fetch", content=dict(type="web_fetch_result",
        url=p.url, retrieved_at=None, content=dict(type="document", title=p.title,
        source=dict(type="text", media_type="text/plain", data=p.text))))

async def test_u85_2_request_shape():
    c = Client(message([report()]))
    await searcher(c).search(AREA, SERVICE)
    [kw] = c.calls
    assert kw["model"] == "test-lookup-model"
    assert kw["tools"][0] == {"type": "web_search_20250305", "name": "web_search", "max_uses": 3,
        "user_location": {"type": "approximate", "city": "Springfield", "region": "Illinois", "country": "US"}}
    assert kw["tools"][1] == {"type": "web_fetch_20250910", "name": "web_fetch", "max_uses": 4, "max_content_tokens": 8000}
    tool = kw["tools"][2]
    assert tool["name"] == "report_utility" and tool["strict"] is True
    assert set(tool["input_schema"]["required"]) == {"found", "company_name", "website_domain", "phone_number"}
    assert tool["input_schema"]["additionalProperties"] is False
    assert kw["tool_choice"] == {"type": "auto"} and kw["output_config"] == {"effort": "medium"}
    assert not {"temperature", "top_p", "top_k", "thinking"} & kw.keys()
    assert all(s in str(kw["messages"][0]["content"]) for s in ("Springfield", "IL", SERVICE))

async def test_u85_2_pause_turn_resumes_without_extra_user_message():
    first = message([dict(type="server_tool_use", id="srv_search", name="web_search", input={"query": "utility"}), search_block()], "pause_turn")
    c = Client(first, message([fetch_block(P_CONTACT), fetch_block(P_START), report()]))
    e = await searcher(c).search(AREA, SERVICE)
    assert len(c.calls) == 2
    assert len(c.calls[1]["messages"]) == 2
    assert c.calls[1]["messages"][0] == c.calls[0]["messages"][0]
    assert c.calls[1]["messages"][1] == {"role": "assistant", "content": first.content}
    assert e.claim == CLAIM and {p.url for p in e.pages} >= {P_CONTACT.url, P_START.url} and e.error is None

async def test_u85_2_pause_turn_limit():
    c = Client(message(stop="pause_turn"))
    assert (await searcher(c).search(AREA, SERVICE)).error == "no_report"
    assert len(c.calls) == TEST_CFG.max_continuations + 1 == 4

async def test_u85_2_citations_add_page_text():
    block = dict(type="text", text="Found the utility.", citations=[dict(type="web_search_result_location",
        url=P_START.url, title="Start", cited_text="To start service call 1-800-555-0199.", encrypted_index="x")])
    e = await searcher(Client(message([block, report()]))).search(AREA, SERVICE)
    assert "1-800-555-0199" in next(p.text for p in e.pages if p.url == P_START.url)

@pytest.mark.parametrize("kind,expected", [("refusal","refusal"), ("max_tokens","truncated"),
    ("end_turn","no_report"), ("timeout","timeout"), ("rate","rate_limited"), ("status","api_error")])
async def test_u85_2_stop_reasons_and_errors_map(kind, expected):
    request = httpx.Request("POST", "https://lookup.example")
    if kind == "timeout":
        reply = anthropic.APITimeoutError(request=request)
    elif kind in ("rate", "status"):
        cls = anthropic.RateLimitError if kind == "rate" else anthropic.APIStatusError
        reply = cls("test error", response=httpx.Response(429 if kind == "rate" else 500, request=request), body={})
    else:
        reply = message(stop=kind)
    assert (await searcher(Client(reply)).search(AREA, SERVICE)).error == expected

def test_u85_2_client_built_from_explicit_settings():
    s = searcher()
    assert s.client.max_retries == 0 and s.client.timeout == 40
    for kw in ({"api_key": ""}, {"timeout_s": 0}):
        with pytest.raises(ValueError):
            searcher(**kw)
