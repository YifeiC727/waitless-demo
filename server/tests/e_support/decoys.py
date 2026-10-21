"""诱饵敏感值的检索（AC-101、AC-107）。

诱饵值来自 conftest 的 decoy_sensitive fixture（或隐藏测试里换过的另一组值）。除了原样字符串，
还检索只保留数字的写法（900-00-0001 → 900000001、1900-01-01 → 19000101），防止被去掉分隔符后漏掉。
"""

import json
import re
from collections.abc import Iterable, Mapping
from typing import Any


def decoy_forms(decoys: Mapping[str, str]) -> dict[str, str]:
    """{原值或其纯数字写法: 字段名}。纯数字写法少于 6 位时不收（太短会误报）。"""
    forms: dict[str, str] = {}
    for key, value in decoys.items():
        forms[value] = key
        digits = re.sub(r"\D", "", value)
        if len(digits) >= 6:
            forms[digits] = key
    return forms


def _as_text(obj: Any) -> str:
    if isinstance(obj, str):
        return obj
    if isinstance(obj, bytes):
        return obj.decode("utf-8", "ignore")
    if hasattr(obj, "model_dump_json"):
        return obj.model_dump_json()
    try:
        return json.dumps(obj, default=str, ensure_ascii=False)
    except (TypeError, ValueError):
        return str(obj)


def find_decoys(haystacks: Iterable[Any], decoys: Mapping[str, str]) -> list[tuple[str, str]]:
    """返回 [(字段名, 命中的写法)]；空列表表示干净。haystack 可以是文本、字节、pydantic 模型或可 JSON 化的对象。"""
    forms = decoy_forms(decoys)
    hits: list[tuple[str, str]] = []
    for hay in haystacks:
        text = _as_text(hay)
        for form, key in forms.items():
            if form in text and (key, form) not in hits:
                hits.append((key, form))
    return hits


__all__ = ["decoy_forms", "find_decoys"]
