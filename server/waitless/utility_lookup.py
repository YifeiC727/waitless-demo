"""L6: public utility lookup; only city and state leave the process."""

import asyncio
import html
import logging
import math
import re
import time
from collections.abc import Sequence
from typing import Annotated, Literal, Protocol
from urllib.parse import urlsplit

import anthropic
import yaml
from pydantic import BaseModel, ConfigDict, Field, PositiveInt

from waitless.interfaces import JevClient
from waitless.decision.jev import build_state
from waitless.models import (SimulatedLine, Skill, Source, JevQuestion, TranscriptLine,
                             NONE_OF_THE_ABOVE, JEV_TRANSCRIPT_MAX_LINES)
from waitless.official_number import phone_numbers_in

LIVE_LOOKUP_SERVICE_KEY = "live-lookup-service"
SITE_QUESTION = "official_site"
NUMBER_QUESTION = "number_purpose"
LOOKUP_REASONS = frozenset({"verified", "no_area", "no_results", "no_official_site",
    "site_low_confidence", "no_report", "not_found", "bad_number", "number_not_on_site",
    "jev_rejected", "jev_error", "timeout", "error", "refusal", "truncated", "rate_limited", "api_error"})

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
    version: Literal[2]
    search_tool: str
    fetch_tool: str
    site_max_searches: PositiveInt
    max_site_candidates: PositiveInt
    max_option_chars: PositiveInt
    max_searches: PositiveInt
    max_fetches: PositiveInt
    max_number_attempts: PositiveInt
    max_content_tokens: PositiveInt
    max_continuations: PositiveInt
    max_tokens: PositiveInt
    effort: Literal["low", "medium", "high"]
    max_sources_shown: PositiveInt
    max_snippet_chars: PositiveInt
    jev_site_min_confidence: Annotated[float, Field(gt=0, le=1)]
    jev_number_min_probability: Annotated[float, Field(gt=0, le=1)]
    site_instructions: Annotated[str, Field(min_length=1)]
    jev_task_goal: Annotated[str, Field(min_length=1)]
    jev_site_instructions: Annotated[str, Field(min_length=1)]
    jev_site_none: Annotated[str, Field(min_length=1)]
    jev_number_instructions: Annotated[str, Field(min_length=1)]
    simulated_line_name: str
    unconfirmed_name: str
    unconfirmed_number: str
    notice_verified: str
    notice_unverified: str
    instructions: str


def load_live_lookup_config(skill: Skill) -> LiveLookupConfig:
    return LiveLookupConfig.model_validate(yaml.safe_load(
        (skill.path / "references/live-lookup.yaml").read_text(encoding="utf-8")))


class SearchResult(_Strict):
    url: str
    title: str


class SiteResults(_Strict):
    results: list[SearchResult]
    error: str | None
    searches: int


class SiteCandidate(_Strict):
    id: str
    site: str
    url: str
    title: str
    description: str


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


def _registrable(domain: str) -> str:
    parts = domain.split(".")
    if len(parts) >= 3 and len(parts[-1]) == 2 and parts[-2] in {"co", "com", "net", "org", "gov", "edu", "ac"}:
        return ".".join(parts[-3:])
    return ".".join(parts[-2:])


def site_candidates(results: Sequence[SearchResult], *, limit: int, max_chars: int) -> list[SiteCandidate]:
    candidates, seen = [], set()
    for result in results:
        if len(candidates) >= limit:
            break
        key = _page_key(result.url)
        site = _domain(key[0]) if key else None
        if site is None:
            continue
        site = _registrable(site)
        if site in seen:
            continue
        seen.add(site)
        host, path = key
        description = " ".join(f"{result.title} - {host}{path}".split())[:max_chars]
        candidates.append(SiteCandidate(id=f"r{len(candidates) + 1}", site=site,
            url=result.url, title=result.title, description=description))
    return candidates


def _unverified(reason: str) -> LookupVerdict:
    return LookupVerdict(verified=False, reason=reason, company_name=None, official_number=None, sources=[])


def _number_pages(evidence: LookupEvidence, site: str, digits: str) -> list[tuple[LookupPage, str]]:
    agreeing, seen = [], set()
    for page in evidence.pages:
        key = _page_key(page.url)
        if key is None or key in seen:
            continue
        seen.add(key)
        host = key[0]
        if host == site or host.endswith("." + site):
            numbers = phone_numbers_in(page.text)
            if digits in numbers:
                agreeing.append((page, numbers[digits]))
    return agreeing


def verify_lookup(evidence: LookupEvidence, *, site: str, config: LiveLookupConfig) -> LookupVerdict:
    if evidence.error:
        return _unverified(evidence.error)
    claim = evidence.claim
    if claim is None:
        return _unverified("no_report")
    if not claim.found or not claim.company_name.strip():
        return _unverified("not_found")
    digits = re.sub(r"\D", "", claim.phone_number)
    if len(digits) == 11 and digits.startswith("1"):
        digits = digits[1:]
    if len(digits) != 10:
        return _unverified("bad_number")
    agreeing = _number_pages(evidence, site, digits)
    if not agreeing:
        return _unverified("number_not_on_site")
    return LookupVerdict(verified=True, reason="on_site", company_name=claim.company_name,
        official_number=agreeing[0][1], sources=[Source(url=p.url, title=p.title or _page_key(p.url)[0])
        for p, _ in agreeing[:config.max_sources_shown]])


class WebSearcher(Protocol):
    async def find_sites(self, area: LookupArea, service: str) -> SiteResults: ...
    async def search(self, area: LookupArea, service: str, *, site: str) -> LookupEvidence: ...


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

    async def find_sites(self, area: LookupArea, service: str) -> SiteResults:
        config = self.config
        found = SiteResults(results=[], error=None, searches=0)
        messages = [{"role": "user", "content": f"{service} in {area.city}, {area.state}, US"}]
        tools = [dict(type=config.search_tool, name="web_search", max_uses=config.site_max_searches,
                      user_location=dict(type="approximate", city=area.city, region=STATES[area.state], country="US"))]
        try:
            for continuation in range(config.max_continuations + 1):
                reply = await self.client.messages.create(model=self.model, max_tokens=config.max_tokens,
                    system=config.site_instructions, messages=messages, tools=tools,
                    tool_choice={"type": "auto"}, output_config={"effort": config.effort})
                for block in reply.content:
                    if _value(block, "type") == "server_tool_use":
                        found.searches += int(_value(block, "name") == "web_search")
                    elif _value(block, "type") == "web_search_tool_result":
                        results = _value(block, "content", [])
                        if isinstance(results, list):
                            for result in results:
                                if _value(result, "type") == "web_search_result":
                                    found.results.append(SearchResult(url=_value(result, "url"), title=_value(result, "title")))
                if reply.stop_reason == "refusal":
                    found.error = "refusal"
                    break
                if reply.stop_reason in ("max_tokens", "model_context_window_exceeded"):
                    found.error = "truncated"
                    break
                if reply.stop_reason != "pause_turn" or continuation == config.max_continuations:
                    break
                messages = [*messages, {"role": "assistant", "content": reply.content}]
        except anthropic.APITimeoutError:
            found.error = "timeout"
        except anthropic.RateLimitError:
            found.error = "rate_limited"
        except Exception:
            found.error = "api_error"
        return found

    async def search(self, area: LookupArea, service: str, *, site: str) -> LookupEvidence:
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

        messages = [{"role": "user", "content": f"{service} in {area.city}, {area.state}, US. Official website: {site}"}]
        schema = LookupClaim.model_json_schema()
        tools = [dict(type=config.search_tool, name="web_search", max_uses=config.max_searches, allowed_domains=[site],
                      user_location=dict(type="approximate", city=area.city, region=STATES[area.state], country="US")),
                 dict(type=config.fetch_tool, name="web_fetch", max_uses=config.max_fetches,
                      max_content_tokens=config.max_content_tokens, allowed_domains=[site]),
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


class _LookupJevError(Exception):
    pass


class LiveLookup:
    def __init__(self, *, searcher: WebSearcher, jev: JevClient, config: LiveLookupConfig, timeout_s: float):
        if not math.isfinite(timeout_s) or timeout_s <= 0 or jev is None:
            raise ValueError("Jev client and positive finite timeout_s are required")
        self.searcher, self.jev, self.config, self.timeout_s = searcher, jev, config, timeout_s
        self._cache = {}

    async def _ask(self, state, question_id, question):
        try:
            return (await self.jev.ask(state, {question_id: question})).answers[question_id]
        except Exception:
            raise _LookupJevError from None

    async def run(self, service: str, addresses: Sequence[str | None]) -> CompanyView:
        started = time.monotonic()
        config = self.config
        area = next((area for address in addresses if (area := area_from_address(address)) is not None), None)
        key = (service, area.city.casefold(), area.state) if area else None
        searches = fetches = candidates = 0
        site_confidence = number_probability = None

        async def lookup():
            nonlocal searches, fetches, candidates, site_confidence, number_probability
            found = await self.searcher.find_sites(area, service)
            searches += found.searches
            if found.error:
                return _unverified(found.error)
            cands = site_candidates(found.results, limit=config.max_site_candidates, max_chars=config.max_option_chars)
            candidates = len(cands)
            if not cands:
                return _unverified("no_results")
            goal = config.jev_task_goal.format(service=service, city=area.city, state=area.state)
            state = build_state(task_goal=goal, allowed_fields={}, standing_rules=[], prior_choices=[], transcript=[])
            q = JevQuestion(type="choice", instructions=config.jev_site_instructions.format(service=service),
                criteria={c.id: c.description for c in cands} | {NONE_OF_THE_ABOVE: config.jev_site_none})
            answer = await self._ask(state, SITE_QUESTION, q)
            site_confidence = answer.confidence
            if answer.choice == NONE_OF_THE_ABOVE:
                return _unverified("no_official_site")
            if site_confidence < config.jev_site_min_confidence:
                return _unverified("site_low_confidence")
            site = next((c.site for c in cands if c.id == answer.choice), None)
            if site is None:
                return _unverified("jev_error")
            for _ in range(config.max_number_attempts):
                evidence = await self.searcher.search(area, service, site=site)
                searches += evidence.searches
                fetches += evidence.fetches
                verdict = verify_lookup(evidence, site=site, config=config)
                if verdict.reason != "number_not_on_site":
                    break
            if verdict.reason != "on_site":
                return verdict
            digits = next(iter(phone_numbers_in(verdict.official_number)))
            lines = []
            for page, number in _number_pages(evidence, site, digits)[:JEV_TRANSCRIPT_MAX_LINES - 1]:
                text = html.unescape(page.text)
                start = max(0, text.find(number) - config.max_snippet_chars // 2)
                window = " ".join(text[start:start + config.max_snippet_chars].split())
                host, path = _page_key(page.url)
                lines.append(TranscriptLine(speaker="company", text=f"{host}{path}: {window}"))
            claim = evidence.claim
            lines.append(TranscriptLine(speaker="waitless", text=f"Lookup claim: company {claim.company_name}; phone number {verdict.official_number}; official website {site}"))
            state = build_state(task_goal=goal, allowed_fields={}, standing_rules=[], prior_choices=[], transcript=lines)
            q = JevQuestion(type="noul", instructions=config.jev_number_instructions.format(service=service))
            number_probability = (await self._ask(state, NUMBER_QUESTION, q)).noul
            if number_probability >= config.jev_number_min_probability:
                return verdict.model_copy(update={"reason": "verified"})
            return _unverified("jev_rejected")

        verdict = self._cache.get(key)
        if verdict is None:
            verdict = _unverified("no_area")
            if area:
                try:
                    verdict = await asyncio.wait_for(lookup(), self.timeout_s)
                except TimeoutError:
                    verdict = _unverified("timeout")
                except _LookupJevError:
                    verdict = _unverified("jev_error")
                except Exception:
                    verdict = _unverified("error")
            if verdict.reason == "verified":
                self._cache[key] = verdict.model_copy(deep=True)
        verified = verdict.reason == "verified"
        notice = config.notice_verified if verified else config.notice_unverified.format(
            area=f"{area.city}, {area.state}" if area else "your area")
        view = CompanyView(name=verdict.company_name if verified else config.unconfirmed_name.format(service=service),
            official_number=verdict.official_number if verified else config.unconfirmed_number,
            sources=verdict.sources if verified else [],
            simulated_line=SimulatedLine(name=config.simulated_line_name, notice=notice))
        reason = verdict.reason if verdict.reason in LOOKUP_REASONS else "error"
        logging.getLogger(__name__).info(
            "lookup status=%s reason=%s searches=%d fetches=%d candidates=%d site_confidence=%s number_probability=%s elapsed_ms=%d",
            "verified" if verified else "unverified", reason, searches, fetches, candidates,
            f"{site_confidence:.2f}" if site_confidence is not None else "na",
            f"{number_probability:.2f}" if number_probability is not None else "na",
            int((time.monotonic() - started) * 1000))
        return view
