"""Keep menu answers diverse in both spoken position and keypad digit."""

import re
from pathlib import Path

import yaml


_DIGIT_WORDS = dict(zip(
    ("zero", "one", "two", "three", "four", "five", "six", "seven", "eight", "nine", "pound", "star"),
    "0123456789#*",
))
_PRESS = re.compile(
    r"\bpress\s+(" + "|".join(_DIGIT_WORDS) + r"|[0-9#*])(?!\w)",
    re.IGNORECASE,
)


def test_menu_correct_answers_vary_in_position_and_key():
    menus_dir = Path(__file__).resolve().parents[3] / "evals" / "menus"
    menus = [yaml.safe_load(path.read_text(encoding="utf-8")) for path in sorted(menus_dir.glob("*.yaml"))]
    assert menus, "No evaluation menus found"
    non_first = 0
    correct_keys = set()
    for menu in menus:
        tokens = [match.group(1).lower() for match in _PRESS.finditer(menu["text"])]
        digits = [_DIGIT_WORDS.get(token, token) for token in tokens]
        correct = menu["correct_digits"]
        assert correct in digits, f"{menu['menu_id']}: correct key {correct!r} is not spoken"
        non_first += digits[0] != correct
        correct_keys.add(correct)
    assert non_first * 2 >= len(menus), f"Only {non_first}/{len(menus)} menus have a non-first correct option"
    assert len(correct_keys) >= 6, f"Only {len(correct_keys)} distinct correct keys"
