"""从菜单文字解析按键选项（G 线；tech §6.5、§6.6；AC-47；K2）。

公开接口（§7）：parse_menu(text, patterns)。patterns 是 navigate-ivr 的 references/ivr-rules.yaml 里的 press_patterns 正则
（经 CompiledCall.press_patterns 带出，E-G-4）。
"""

from collections.abc import Mapping, Sequence
import re

from waitless.models import MenuOption

__all__ = ["DEFAULT_DIGIT_WORDS", "MENU_LABEL_MAX", "parse_menu", "truncate_label"]

MENU_LABEL_MAX = 80  # §6.6：criteria 的标签截断到 80 字

# 缺口 G-G-03（定稿 optional_kwarg_with_default）：不传 digit_words 时用的标准数字词映射。
DEFAULT_DIGIT_WORDS: dict[str, str] = {
    "zero": "0",
    "one": "1",
    "two": "2",
    "three": "3",
    "four": "4",
    "five": "5",
    "six": "6",
    "seven": "7",
    "eight": "8",
    "nine": "9",
    "star": "*",
    "pound": "#",
    "hash": "#",
}


def parse_menu(text: str, patterns: Sequence[str], *, digit_words: Mapping[str, str] | None = None) -> list[MenuOption]:
    """按 patterns 顺序匹配「to <label> press <digits>」「press <digits> for <label>」等写法；
    同一 digits 只保留第一次出现；数字词（one、nine、pound 等）按 digit_words 转成按键：不给时用
    DEFAULT_DIGIT_WORDS，给时用它（ivr-rules.yaml 的 digit_words，经 CompiledCall.digit_words 带出）。
    D 的 TurnDetector、E 的 TextCall 现有的两参数调用不变。
    解析不出任何选项时返回 []（TurnDetector 据此把这一轮当 speech）。"""
    words = DEFAULT_DIGIT_WORDS if digit_words is None else digit_words
    matches = []
    for index, pattern in enumerate(patterns):
        for match in re.finditer(pattern, text, flags=re.IGNORECASE):
            groups = match.groupdict()
            raw_digits = groups.get("digits", "").strip().lower()
            if re.fullmatch(r"[0-9*#]+", raw_digits):
                digits = raw_digits
            else:
                digits = words.get(raw_digits)
            label = groups.get("label", "").strip(" ,:;\t\r\n")
            if digits and label and re.fullmatch(r"[0-9*#]+", digits):
                matches.append((match.start(), index, digits, label))
    options, seen = [], set()
    for _, _, digits, label in sorted(matches):
        if digits not in seen:
            seen.add(digits)
            options.append(MenuOption(digits=digits, label=truncate_label(label)))
    return options


def truncate_label(label: str, limit: int = MENU_LABEL_MAX) -> str:
    if limit < 0:
        raise ValueError("label limit must be nonnegative")
    return label[:limit]
