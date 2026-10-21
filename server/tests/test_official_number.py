"""AC-26：find_official_number 只采信 trusted-sources.yaml 所列域名的来源；号码一致的来源少于 2 个时 verified=false；
非官方域名的来源不计入。

机制（tech §7.1）：来源域名必须在 companies.<id>.official_domains 里，且至少 min_agreeing_sources（2）个来源号码一致；
FixtureFetcher 把 <domain>/<path> 映射到 company/<id>/www/<path> 的本地文件，不出网。
trusted_sources 的键名按 §6.9 暂定读法（见 a_support.trusted_sources 与测试计划第 5 节）。
"""

import re

import yaml

from waitless.official_number import FixtureFetcher, find_official_number

from a_support import LAKESHORE_DOMAIN, LAKESHORE_NUMBER, LAKESHORE_TRUSTED, LAKESHORE_URLS, DictFetcher, lakeshore_pages, trusted_sources

THIRD_PARTY = "https://utility-directory.example/lakeshore-energy"


def _digits(s):
    return re.sub(r"\D", "", s or "")


def _trusted(extra_sources=()):
    companies = {"lakeshore-energy": {**LAKESHORE_TRUSTED["lakeshore-energy"],
                                      "sources": [*LAKESHORE_TRUSTED["lakeshore-energy"]["sources"], *extra_sources]}}
    return trusted_sources(companies)


def test_ac26_two_official_sources_agree_is_verified():
    out = find_official_number("Lakeshore Energy", "start service", DictFetcher(lakeshore_pages()),
                               trusted_sources=_trusted())
    assert out.verified is True
    assert out.company_id == "lakeshore-energy"
    assert _digits(out.official_number) == _digits(LAKESHORE_NUMBER)
    found = {s.url: s.number_found for s in out.sources}
    assert set(LAKESHORE_URLS) <= set(found)
    assert all(_digits(found[u]) == _digits(LAKESHORE_NUMBER) for u in LAKESHORE_URLS)


def test_ac26_only_one_official_source_with_the_number_is_not_verified():
    pages = lakeshore_pages()
    pages[LAKESHORE_URLS[1]] = "<html><body><p>Start service online.</p></body></html>"  # 这一页没有号码
    out = find_official_number("Lakeshore Energy", "start service", DictFetcher(pages), trusted_sources=_trusted())
    assert out.verified is False
    assert out.reason.strip()


def test_ac26_non_official_domain_is_listed_but_not_counted():
    pages = lakeshore_pages()
    pages[LAKESHORE_URLS[1]] = "<html><body><p>Start service online.</p></body></html>"
    pages[THIRD_PARTY] = f"<p>Lakeshore Energy phone: {LAKESHORE_NUMBER}</p>"
    out = find_official_number("Lakeshore Energy", "start service", DictFetcher(pages),
                               trusted_sources=_trusted([{"url": THIRD_PARTY, "title": "Utility directory"}]))
    assert out.verified is False, "an official page plus a third-party page is not two official sources"
    assert THIRD_PARTY in {s.url for s in out.sources}


def test_ac26_unknown_company_has_no_id_or_number():
    out = find_official_number("Northwind Gas", "cancel service", DictFetcher(lakeshore_pages()),
                               trusted_sources=_trusted())
    assert out.company_id is None
    assert out.official_number is None
    assert out.verified is False


def test_ac26_fixture_fetcher_reads_company_www_without_network(tmp_path):
    company = tmp_path / "company" / "lakeshore-energy"
    (company / "www").mkdir(parents=True)
    (company / "company.yaml").write_text(yaml.safe_dump({  # §6.10 的字段；FixtureFetcher 只需 website
        "company_id": "lakeshore-energy", "name": "Lakeshore Energy", "line_number": LAKESHORE_NUMBER,
        "website": {"domain": LAKESHORE_DOMAIN, "dir": "www"},
        "voices": {"ivr_polly": "Joanna", "agent_sonic": "tiffany", "rep_sonic": "matthew"},
        "customers": [{"account_number": "8800123456", "name": "Alex Example", "phone_number": "(312) 555-0147",
                       "address": "1200 W Example St, Apt 4B"}],
        "menu": {"timeout_sec": 8, "max_timeouts": 2},
    }), encoding="utf-8")
    (company / "www" / "contact.html").write_text("<p>Customer service: (312) 555-0100</p>", encoding="utf-8")
    fetcher = FixtureFetcher.from_company_dir(tmp_path / "company")
    assert fetcher.fetch(f"https://{LAKESHORE_DOMAIN}/contact.html") == "<p>Customer service: (312) 555-0100</p>"
    assert fetcher.fetch(f"https://{LAKESHORE_DOMAIN}/missing.html") is None
    assert fetcher.fetch("https://other-company.example/contact.html") is None
