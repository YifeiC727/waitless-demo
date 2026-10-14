"""查官方号码：只认受信域名、至少两源一致（A 线；tech §6.1 find_official_number、§6.9；AC-26、R51）。

公开接口（§7）：find_official_number(company, intent, fetcher)、FixtureFetcher。
默认（含 replay 与测试）只读本地夹具 company/<id>/www/，不出网。
"""

import html
import re
import yaml
from urllib.parse import urlsplit, unquote
from waitless.models import SourceCheck

from collections.abc import Mapping
from pathlib import Path
from typing import Any, Protocol

from waitless.models import OfficialNumberOut, TrustedSources

__all__ = ["Fetcher", "FixtureFetcher", "find_official_number"]


class Fetcher(Protocol):
    def fetch(self, url: str) -> str | None:
        """返回页面文本；取不到返回 None。"""
        parsed = urlsplit(url)
        root = self.roots.get((parsed.hostname or "").casefold())
        if root is None or parsed.scheme not in ("http", "https"):
            return None
        path = (root / unquote(parsed.path).lstrip("/")).resolve()
        if not path.is_relative_to(root):
            return None
        try:
            return path.read_text(encoding="utf-8")
        except (OSError, UnicodeError):
            return None


class FixtureFetcher:
    """把 `<domain>/<path>` 映射到 company/<id>/www/<path> 的本地文件（域名取 company.yaml 的 website.domain）。"""

    def __init__(self, roots: Mapping[str, Path]) -> None:
        """roots：域名 → 本地 www 目录。"""
        self.roots = {domain.casefold(): Path(root).resolve() for domain, root in roots.items()}

    @classmethod
    def from_company_dir(cls, company_dir: Path) -> "FixtureFetcher":
        # todo(A): 遍历 company_dir/*/company.yaml，用 website.domain 与 website.dir 建映射。
        # todo(A): 缺口 A-G-13（定稿 raw_yaml，暂定）：只用 yaml.safe_load 读这两个键，不经 B 的 load_company /
        #          CompanyConfig（A 不依赖 B 的进度；company.yaml 其他字段写错不在这里报）。
        roots = {}
        for config_path in sorted(company_dir.glob("*/company.yaml")):
            data = yaml.safe_load(config_path.read_text(encoding="utf-8"))
            website = data["website"]
            root = (config_path.parent / website["dir"]).resolve()
            if not root.is_relative_to(config_path.parent.resolve()):
                raise ValueError("website directory escapes company directory")
            roots[website["domain"]] = root
        return cls(roots)

    def fetch(self, url: str) -> str | None:
        parsed = urlsplit(url)
        root = self.roots.get((parsed.hostname or "").casefold())
        if root is None or parsed.scheme not in ("http", "https"):
            return None
        path = (root / unquote(parsed.path).lstrip("/")).resolve()
        if not path.is_relative_to(root):
            return None
        try:
            return path.read_text(encoding="utf-8")
        except (OSError, UnicodeError):
            return None


def find_official_number(
    company: str,
    intent: str,
    fetcher: Fetcher,
    *,
    trusted_sources: TrustedSources | Mapping[str, Any],
) -> OfficialNumberOut:
    """trusted_sources 是 find-official-number skill 的 references/trusted-sources.yaml 内容；
    传字典时先按 models.TrustedSources 校验（缺口 A-G-4，定稿 frozen_models）。"""
    trusted = TrustedSources.model_validate(trusted_sources) if not isinstance(trusted_sources, TrustedSources) else trusted_sources
    match = next(((cid, info) for cid, info in trusted.companies.items()
                  if company.strip().casefold() in (cid.casefold(), info.name.casefold())), None)
    if match is None:
        return OfficialNumberOut(company_id=None, company_name=company, official_number=None, verified=False,
                                 sources=[], reason="Company has no trusted official sources.")
    company_id, info = match
    sources, votes, numbers = [], {}, {}
    for source in info.sources:
        domain = (urlsplit(source.url).hostname or "").casefold()
        official = domain in {d.casefold() for d in info.official_domains}
        text = fetcher.fetch(source.url)
        found = None
        if text:
            candidates = re.findall(r"(?<![\d])(?:\+?1[\s.-]*)?(?:\(\d{3}\)|\d{3})[\s.-]*\d{3}[\s.-]*\d{4}(?![\d])", html.unescape(text))
            unique = {re.sub(r"\D", "", n).removeprefix("1") if len(re.sub(r"\D", "", n)) == 11 else re.sub(r"\D", "", n): n for n in candidates}
            if len(unique) == 1:
                digits, found = next(iter(unique.items()))
                if official:
                    votes.setdefault(digits, set()).add(source.url)
                    numbers[digits] = found
        sources.append(SourceCheck(url=source.url, title=source.title,
                                   number_found=found if official else None))
    winners = [n for n, urls in votes.items() if len(urls) >= trusted.min_agreeing_sources]
    verified = len(winners) == 1
    number = numbers[winners[0]] if verified else None
    return OfficialNumberOut(company_id=company_id, company_name=info.name, official_number=number,
                             verified=verified, sources=sources,
                             reason="Official sources agree." if verified else "Official number could not be verified by agreeing independent sources.")
