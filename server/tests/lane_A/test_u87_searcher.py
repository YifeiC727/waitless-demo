import anthropic
import httpx
import pytest
from .test_u85_anthropic_searcher import Client, message, report, searcher
from .test_u85_verify import AREA, SERVICE
from .u87_support import SITE, R_DIR, R_OWN

def results_block(*rs):
    return {"type": "web_search_tool_result", "tool_use_id": "srv_1", "content": [{"type": "web_search_result", "url": r.url, "title": r.title, "encrypted_content": "x"} for r in rs]}

LOCATION = {"type": "approximate", "city": "Springfield", "region": "Illinois", "country": "US"}

async def test_u87_1_find_sites_request_shape():
    c = Client(message([{"type": "server_tool_use", "id": "srv_1", "name": "web_search", "input": {"query": "q"}}, results_block(R_DIR, R_OWN)], "end_turn"))
    out = await searcher(c).find_sites(AREA, SERVICE)
    [kw] = c.calls
    assert kw["tools"] == [{"type": "web_search_20250305", "name": "web_search", "max_uses": 2, "user_location": LOCATION}]
    assert kw["system"] == "SEARCH-ONLY"
    assert kw["messages"] == [{"role": "user", "content": "electric utility in Springfield, IL, US"}]
    assert kw["tool_choice"] == {"type": "auto"} and kw["output_config"] == {"effort": "medium"}
    assert not {"temperature", "top_p", "top_k", "thinking"} & kw.keys()
    assert out.results == [R_DIR, R_OWN] and out.searches == 1 and out.error is None

async def test_u87_1_find_sites_resumes_after_pause_turn():
    first = message([{"type": "server_tool_use", "id": "srv_1", "name": "web_search", "input": {"query": "q"}}, results_block(R_DIR)], "pause_turn")
    c = Client(first, message([results_block(R_OWN)], "end_turn"))
    out = await searcher(c).find_sites(AREA, SERVICE)
    assert len(c.calls) == 2
    assert c.calls[1]["messages"] == [c.calls[0]["messages"][0], {"role": "assistant", "content": first.content}]
    assert out.results == [R_DIR, R_OWN]

async def test_u87_1_find_sites_keeps_results_when_continuations_run_out():
    c = Client(message([results_block(R_OWN)], "pause_turn"))
    out = await searcher(c).find_sites(AREA, SERVICE)
    assert len(c.calls) == 4 and out.error is None and out.results[0] == R_OWN

@pytest.mark.parametrize("kind,expected", [("refusal", "refusal"), ("max_tokens", "truncated"), ("timeout", "timeout"), ("rate", "rate_limited"), ("status", "api_error")])
async def test_u87_1_find_sites_errors_map(kind, expected):
    request = httpx.Request("POST", "https://lookup.example")
    if kind == "timeout":
        reply = anthropic.APITimeoutError(request=request)
    elif kind in ("rate", "status"):
        cls = anthropic.RateLimitError if kind == "rate" else anthropic.APIStatusError
        reply = cls("test error", response=httpx.Response(429 if kind == "rate" else 500, request=request), body={})
    else:
        reply = message(stop=kind)
    assert (await searcher(Client(reply)).find_sites(AREA, SERVICE)).error == expected

async def test_u87_1_site_search_request_shape():
    c = Client(message([report()]))
    await searcher(c).search(AREA, SERVICE, site=SITE)
    [kw] = c.calls
    assert kw["model"] == "test-lookup-model"
    assert kw["tools"][0] == {"type": "web_search_20250305", "name": "web_search", "max_uses": 2, "user_location": LOCATION, "allowed_domains": ["prairie-light.example"]}
    assert kw["tools"][1] == {"type": "web_fetch_20250910", "name": "web_fetch", "max_uses": 2, "max_content_tokens": 8000, "allowed_domains": ["prairie-light.example"]}
    tool = kw["tools"][2]
    assert tool["name"] == "report_utility" and tool["strict"] is True
    assert set(tool["input_schema"]["required"]) == {"found", "company_name", "website_domain", "phone_number"}
    assert tool["input_schema"]["additionalProperties"] is False
    assert kw["system"] == "ON-SITE"
    assert kw["messages"] == [{"role": "user", "content": "electric utility in Springfield, IL, US. Official website: prairie-light.example"}]
