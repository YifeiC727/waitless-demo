"""I-3b 补测 · 仓库里的评测数据（第 3 步可见组，冻结）：AC-90、AC-85（test-gaps G22；E 单 R11、R12）。

- `evals/menus/`：16 个手写变体菜单（虚构品牌），每个都能被 navigate-ivr 的 press_patterns 解析出至少 2 个选项、其中有正确按键
  （解析不出正确键时，这条菜单在真实评测里必错，测的就成了解析器而不是 Jev）；合起来覆盖 R11 列的写法：「To X, press N」
  「Press N for/to X」「For X, press N」、数字词、星号或井号、0 转人工、9 重听、和目标相近的干扰项（start 对 move）。
- `cases.extra.jsonl` 里的 round 用例引用场景台词时逐字一致（R12「台词以合入后的场景文件为准」），场景改了台词用例就会跟着失败。
直接读文件、调 G 的公开函数 parse_menu；不经被测的 load_menus、load_cases。
"""

import json
import re

import yaml

from waitless import models as m
from waitless.decision.menu_parse import parse_menu

DIGIT_WORD = re.compile(r"\bpress\s+(zero|one|two|three|four|five|six|seven|eight|nine)\b", re.IGNORECASE)
STAR_POUND = re.compile(r"\bpress\s+(star|pound|hash|\*|#)", re.IGNORECASE)
FORMS = {
    "To X, press N": re.compile(r"\bTo\s+[^.,;]+,\s*press\s+\S", re.IGNORECASE),
    "Press N for/to X": re.compile(r"\bPress\s+\S+\s+(for|to)\s+", re.IGNORECASE),
    "For X, press N": re.compile(r"\bFor\s+[^.,;]+,\s*press\s+\S", re.IGNORECASE),
}


def _menus(menus_dir):
    return [m.MenuVariant.model_validate(yaml.safe_load(p.read_text(encoding="utf-8"))) for p in sorted(menus_dir.glob("*.yaml"))]


def _keys(text, rules):
    return [o.digits for o in parse_menu(text, rules["press_patterns"], digit_words=rules.get("digit_words"))]


def test_ac90_i3b_repo_menus_parse_and_cover_the_required_phrasings(menus_dir, skills_dir):
    rules = yaml.safe_load((skills_dir / "navigate-ivr" / "references" / "ivr-rules.yaml").read_text(encoding="utf-8"))
    menus = _menus(menus_dir)
    assert len(menus) >= 16, f"R11 asks for 16 menus, found {len(menus)}"
    offered = []
    for v in menus:
        keys = _keys(v.text, rules)
        assert v.correct_digits in keys and len(set(keys)) >= 2, f"{v.menu_id}: parsed {keys} from {v.text!r}"
        assert "lakeshore" not in v.text.lower(), v.menu_id
        offered.append(set(keys))
    texts = [v.text for v in menus]
    for name, rx in FORMS.items():
        assert any(rx.search(t) for t in texts), f"no menu uses the {name!r} wording"
    assert any(DIGIT_WORD.search(t) for t in texts), "no menu says a digit as a word (press two)"
    assert any(STAR_POUND.search(t) for t in texts), "no menu offers star or pound"
    assert any("0" in k for k in offered), "no menu offers 0 for an operator"
    assert any("9" in k for k in offered), "no menu offers 9 to repeat"
    assert any("start" in v.goal.lower() and "move" in v.text.lower() for v in menus), "no start-service menu with a nearby move distractor"


def test_ac85_i3b_repo_extra_round_cases_quote_the_scenario_lines(skills_dir, company_dir):
    lines = set()
    for path in sorted((company_dir / "lakeshore-energy" / "scenarios").glob("*.yaml")):
        s = m.Scenario.model_validate(yaml.safe_load(path.read_text(encoding="utf-8")))
        for st in s.steps.values():
            if st.text:
                lines.add(st.text.strip())
            lines.update(ln.say.strip() for ln in (st.text_script or []))
    extra = []
    for path in sorted(skills_dir.glob("*/evals/cases.extra.jsonl")):
        extra += [m.EvalCase.model_validate(json.loads(raw)) for raw in path.read_text(encoding="utf-8").splitlines() if raw.strip()]
    rounds = [c for c in extra if c.kind == "round"]
    assert len(rounds) >= 3, "R12 adds three round cases taken from the new scenarios"
    for c in rounds:
        assert (c.input.get("text") or "").strip() in lines, f"{c.case_id} quotes a line that is not in any scenario file"
