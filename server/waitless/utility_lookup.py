"""L6: public utility lookup; only city and state leave the process."""

import asyncio
import html
import logging
import math
import re
import time
from collections.abc import Sequence
from typing import Literal, Protocol
from urllib.parse import urlsplit

import anthropic
import yaml
from pydantic import BaseModel, ConfigDict, PositiveInt

from waitless.models import SimulatedLine, Skill, Source
from waitless.official_number import phone_numbers_in

LIVE_LOOKUP_SERVICE_KEY = "live-lookup-service"

# Postal abbreviations and region names for the approximate search location.
STATES = dict(zip(
    "AL AK AZ AR CA CO CT DE FL GA HI ID IL IN IA KS KY LA ME MD MA MI MN MS MO MT NE NV NH NJ NM NY NC ND OH OK OR PA RI SC SD TN TX UT VT VA WA WV WI WY DC".split(),
    ("Alabama|Alaska|Arizona|Arkansas|California|Colorado|Connecticut|Delaware|Florida|Georgia|Hawaii|Idaho|Illinois|Indiana|Iowa|Kansas|Kentucky|Louisiana|Maine|Maryland|Massachusetts|Michigan|Minnesota|Mississippi|Missouri|Montana|Nebraska|Nevada|New Hampshire|New Jersey|New Mexico|New York|North Carolina|North Dakota|Ohio|Oklahoma|Oregon|Pennsylvania|Rhode Island|South Carolina|South Dakota|Tennessee|Texas|Utah|Vermont|Virginia|Washington|West Virginia|Wisconsin|Wyoming|District of Columbia").split("|"),
    strict=True))
_AREA = re.compile(r"(?:^|,)\s*([A-Za-z .'-]{1,40})\s*,\s*([A-Za-z]{2})(?:\s+\d{5}(?:-\d{4})?)?(?:\s*,\s*(?:US|USA|United States))?\s*$", re.I)


class _Strict(BaseModel):
    model_config = ConfigDict(extra="forbid")


class LookupArea(_Strict):
    city: str
    state: str


def area_from_address(address: str | None) -> LookupArea | None:
    if not address:
        return None
    match = _AREA.search(" ".join(address.split()))
    if match and match[2].upper() in STATES and re.search(r"[A-Za-z]", match[1]):
        return LookupArea(city=match[1].strip(), state=match[2].upper())
    return None


class LiveLookupConfig(_Strict):
    version: Literal[1]
    search_tool: str
    fetch_tool: str
    max_searches: PositiveInt
    max_fetches: PositiveInt
    max_content_tokens: PositiveInt
    max_continuations: PositiveInt
    max_tokens: PositiveInt
    effort: Literal["low", "medium", "high"]
    extra_official_suffixes: list[str]
    max_sources_shown: PositiveInt
    simulated_line_name: str
    unconfirmed_name: str
    unconfirmed_number: str
    notice_verified: str
    notice_unverified: str
    instructions: str


def load_live_lookup_config(skill: Skill) -> LiveLookupConfig:
    return LiveLookupConfig.model_validate(yaml.safe_load(
        (skill.path / "references/live-lookup.yaml").read_text(encoding="utf-8")))


class LookupPage(_Strict):
    url: str
    title: str
    text: str


class LookupClaim(_Strict):
    found: bool
    company_name: str
    website_domain: str
    phone_number: str


class LookupEvidence(_Strict):
    claim: LookupClaim | None
    pages: list[LookupPage]
    error: str | None
    searches: int
    fetches: int


class LookupVerdict(_Strict):
    verified: bool
    reason: str
    company_name: str | None
    official_number: str | None
    sources: list[Source]


def _page_key(url: str) -> tuple[str, str] | None:
    try:
        parsed = urlsplit(url)
        if parsed.scheme in ("http", "https") and parsed.hostname:
            return parsed.hostname.casefold(), parsed.path or "/"
    except ValueError:
        pass
    return None


def _domain(value: str) -> str | None:
    try:
        parsed = urlsplit(value if urlsplit(value).scheme else "//" + value)
        domain = (parsed.hostname or "").casefold().removeprefix("www.")
        if parsed.scheme not in ("", "http", "https") or parsed.username or parsed.password or parsed.port:
            return None
        if re.fullmatch(r"(?:[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?\.)+[a-z]{2,63}", domain):
            return domain
    except ValueError:
        pass
    return None


def _normalized(text: str) -> str:
    return " ".join(html.unescape(text).casefold().split())


def verify_lookup(evidence: LookupEvidence, *, min_agreeing_sources: int,
                  config: LiveLookupConfig) -> LookupVerdict:
    def no(reason):
        return LookupVerdict(verified=False, reason=reason, company_name=None, official_number=None, sources=[])
    if evidence.error:
        return no(evidence.error)
    claim = evidence.claim
    if claim is None:
        return no("no_report")
    if not claim.found:
        return no("not_found")
    digits = re.sub(r"\D", "", claim.phone_number)
    if len(digits) == 11 and digits.startswith("1"):
        digits = digits[1:]
    if len(digits) != 10:
        return no("bad_number")
    domain = _domain(claim.website_domain)
    if domain is None:
        return no("bad_domain")
    agreeing, seen = [], set()
    for page in evidence.pages:
        key = _page_key(page.url)
        if key is None or key in seen:
            continue
        seen.add(key)
        host = key[0]
        own = host == domain or host.endswith("." + domain)
        extra = any(host.endswith(suffix.casefold()) for suffix in config.extra_official_suffixes if suffix.startswith("."))
        numbers = phone_numbers_in(page.text)
        if (own or extra) and digits in numbers:
            agreeing.append((own, page, numbers[digits]))
    agreeing.sort(key=lambda entry: not entry[0])
    if len(agreeing) < min_agreeing_sources or not any(own for own, _, _ in agreeing):
        return no("too_few_sources")
    name = _normalized(claim.company_name)
    if not name or not any(name in _normalized(page.text) for _, page, _ in agreeing):
        return no("name_not_on_source")
    return LookupVerdict(verified=True, reason="verified", company_name=claim.company_name,
        official_number=agreeing[0][2], sources=[Source(url=p.url, title=p.title or _page_key(p.url)[0])
        for _, p, _ in agreeing[:config.max_sources_shown]])


class WebSearcher(Protocol):
    async def search(self, area: LookupArea, service: str) -> LookupEvidence: ...


def _value(obj, key, default=None):
    return obj.get(key, default) if isinstance(obj, dict) else getattr(obj, key, default)


class AnthropicWebSearcher:
    def __init__(self, *, api_key, base_url, timeout_s, model, config: LiveLookupConfig, client=None):
        try:
            parsed = urlsplit(base_url or "")
            valid = bool(api_key and api_key.strip() and model and model.strip()
                         and parsed.scheme in ("http", "https") and parsed.hostname
                         and timeout_s is not None and math.isfinite(timeout_s) and timeout_s > 0)
        except (ValueError, TypeError):
            valid = False
        if not valid:
            raise ValueError("explicit credentials, base_url, model and positive finite timeout_s are required")
        self.client = client if client is not None else anthropic.AsyncAnthropic(
            api_key=api_key, base_url=base_url, timeout=timeout_s, max_retries=0)
        self.model, self.config = model, config

    async def search(self, area: LookupArea, service: str) -> LookupEvidence:
        config = self.config
        evidence = LookupEvidence(claim=None, pages=[], error=None, searches=0, fetches=0)
        pages, titles = {}, {}

        def add_page(url, title, text):
            key = _page_key(url or "")
            if key is None or not isinstance(text, str):
                return
            if key not in pages:
                pages[key] = LookupPage(url=url, title=title or "", text=text)
            else:
                page = pages[key]
                page.text += "\n" + text
                if title:
                    page.title = title

        def collect(blocks):
            for block in blocks:
                kind = _value(block, "type")
                if kind == "server_tool_use":
                    name = _value(block, "name")
                    evidence.searches += int(name == "web_search")
                    evidence.fetches += int(name == "web_fetch")
                elif kind == "web_search_tool_result":
                    results = _value(block, "content", [])
                    if isinstance(results, list):
                        for result in results:
                            key = _page_key(_value(result, "url", ""))
                            if key:
                                titles[key] = _value(result, "title", "")
                elif kind == "web_fetch_tool_result":
                    result = _value(block, "content")
                    if _value(result, "type") == "web_fetch_result":
                        document = _value(result, "content")
                        source = _value(document, "source")
                        if _value(source, "type") == "text":
                            add_page(_value(result, "url"), _value(document, "title"), _value(source, "data"))
                elif kind == "text":
                    for citation in _value(block, "citations", []) or []:
                        if _value(citation, "type") == "web_search_result_location":
                            add_page(_value(citation, "url"), None, _value(citation, "cited_text"))
                            key = _page_key(_value(citation, "url", ""))
                            if key and not titles.get(key):
                                titles[key] = _value(citation, "title", "")

        messages = [{"role": "user", "content": f"{service} in {area.city}, {area.state}, US"}]
        schema = LookupClaim.model_json_schema()
        tools = [dict(type=config.search_tool, name="web_search", max_uses=config.max_searches,
                      user_location=dict(type="approximate", city=area.city, region=STATES[area.state], country="US")),
                 dict(type=config.fetch_tool, name="web_fetch", max_uses=config.max_fetches,
                      max_content_tokens=config.max_content_tokens),
                 dict(name="report_utility", strict=True, input_schema=schema)]
        try:
            for continuation in range(config.max_continuations + 1):
                reply = await self.client.messages.create(model=self.model, max_tokens=config.max_tokens,
                    system=config.instructions, messages=messages, tools=tools,
                    tool_choice={"type": "auto"}, output_config={"effort": config.effort})
                collect(reply.content)
                if reply.stop_reason == "refusal":
                    evidence.error = "refusal"
                    break
                if reply.stop_reason in ("max_tokens", "model_context_window_exceeded"):
                    evidence.error = "truncated"
                    break
                if reply.stop_reason not in ("pause_turn", "tool_use", "end_turn"):
                    evidence.error = "api_error"
                    break
                reports = [b for b in reply.content if _value(b, "type") == "tool_use"
                           and _value(b, "name") == "report_utility"]
                if reports:
                    try:
                        evidence.claim = LookupClaim.model_validate(_value(reports[0], "input"))
                    except ValueError:
                        evidence.error = "no_report"
                    break
                if reply.stop_reason != "pause_turn" or continuation == config.max_continuations:
                    evidence.error = "no_report"
                    break
                messages = [*messages, {"role": "assistant", "content": reply.content}]
        except anthropic.APITimeoutError:
            evidence.error = "timeout"
        except anthropic.RateLimitError:
            evidence.error = "rate_limited"
        except Exception:
            evidence.error = "api_error"
        for key, page in pages.items():
            page.title = page.title or titles.get(key) or key[0]
        evidence.pages = list(pages.values())
        return evidence


class CompanyView(_Strict):
    name: str
    official_number: str
    sources: list[Source]
    simulated_line: SimulatedLine


class LiveLookup:
    def __init__(self, *, searcher: WebSearcher, config: LiveLookupConfig,
                 min_agreeing_sources: int, timeout_s: float):
        if not math.isfinite(timeout_s) or timeout_s <= 0 or min_agreeing_sources <= 0:
            raise ValueError("positive source count and finite timeout_s are required")
        self.searcher, self.config = searcher, config
        self.min_agreeing_sources, self.timeout_s = min_agreeing_sources, timeout_s
        self._cache = {}

    async def run(self, service: str, addresses: Sequence[str | None]) -> CompanyView:
        started = time.monotonic()
        area = next((area for address in addresses if (area := area_from_address(address)) is not None), None)
        key = (service, area.city.casefold(), area.state) if area else None
        evidence = LookupEvidence(claim=None, pages=[], error="no_area", searches=0, fetches=0)
        verdict = self._cache.get(key)
        if verdict is None:
            if area:
                try:
                    evidence = await asyncio.wait_for(self.searcher.search(area, service), self.timeout_s)
                except TimeoutError:
                    evidence.error = "timeout"
                except Exception:
                    evidence.error = "error"
            verdict = verify_lookup(evidence, min_agreeing_sources=self.min_agreeing_sources, config=self.config)
            if verdict.verified:
                self._cache[key] = verdict.model_copy(deep=True)
        config = self.config
        notice = config.notice_verified if verdict.verified else config.notice_unverified.format(
            area=f"{area.city}, {area.state}" if area else "your area")
        view = CompanyView(name=verdict.company_name if verdict.verified else config.unconfirmed_name.format(service=service),
            official_number=verdict.official_number if verdict.verified else config.unconfirmed_number,
            sources=verdict.sources if verdict.verified else [],
            simulated_line=SimulatedLine(name=config.simulated_line_name, notice=notice))
        # Restrict log reasons to program-defined codes, never searcher/model output.
        reason = verdict.reason if verdict.reason in {"verified", "no_area", "no_report", "not_found", "bad_number",
            "bad_domain", "too_few_sources", "name_not_on_source", "timeout", "error", "refusal", "truncated",
            "rate_limited", "api_error"} else "error"
        logging.getLogger(__name__).info("lookup status=%s reason=%s searches=%d fetches=%d elapsed_ms=%d",
            "verified" if verdict.verified else "unverified", reason, evidence.searches, evidence.fetches,
            int((time.monotonic() - started) * 1000))
        return view
