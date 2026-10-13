"""查官方号码：只认受信域名、至少两源一致（A 线；tech §6.1 find_official_number、§6.9；AC-26、R51）。

公开接口（§7）：find_official_number(company, intent, fetcher)、FixtureFetcher。
默认（含 replay 与测试）只读本地夹具 company/<id>/www/，不出网。
"""

from collections.abc import Mapping
from pathlib import Path
from typing import Any, Protocol

from waitless.models import OfficialNumberOut, TrustedSources

__all__ = ["Fetcher", "FixtureFetcher", "find_official_number"]


class Fetcher(Protocol):
    def fetch(self, url: str) -> str | None:
        """返回页面文本；取不到返回 None。"""
        raise NotImplementedError


class FixtureFetcher:
    """把 `<domain>/<path>` 映射到 company/<id>/www/<path> 的本地文件（域名取 company.yaml 的 website.domain）。"""

    def __init__(self, roots: Mapping[str, Path]) -> None:
        """roots：域名 → 本地 www 目录。"""
        raise NotImplementedError

    @classmethod
    def from_company_dir(cls, company_dir: Path) -> "FixtureFetcher":
        # todo(A): 遍历 company_dir/*/company.yaml，用 website.domain 与 website.dir 建映射。
        # todo(A): 缺口 A-G-13（定稿 raw_yaml，暂定）：只用 yaml.safe_load 读这两个键，不经 B 的 load_company /
        #          CompanyConfig（A 不依赖 B 的进度；company.yaml 其他字段写错不在这里报）。
        raise NotImplementedError

    def fetch(self, url: str) -> str | None:
        raise NotImplementedError


def find_official_number(
    company: str,
    intent: str,
    fetcher: Fetcher,
    *,
    trusted_sources: TrustedSources | Mapping[str, Any],
) -> OfficialNumberOut:
    """trusted_sources 是 find-official-number skill 的 references/trusted-sources.yaml 内容；
    传字典时先按 models.TrustedSources 校验（缺口 A-G-4，定稿 frozen_models）。"""
    # todo(A): 只采信 companies.<id>.official_domains 里的域名；非官方域名的来源列出但不计入（AC-26）。
    # todo(A): 号码一致的来源少于 min_agreeing_sources（2）时 verified=false，reason 说明原因。
    # todo(A): 公司不在名单里时 company_id=null、official_number=null。
    raise NotImplementedError
