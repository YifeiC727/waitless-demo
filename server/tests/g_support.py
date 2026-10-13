"""G 线（技能与决策门）测试的共用构造器（第 3 步写；可见组与隐藏组共用；冻结，各线不改）。

不是测试文件，pytest 不收集。被测代码只经 tech §6、§7 的公开名字访问：
  skills/loader.py、skills/compiler.py、decision/{jev,registry,policy,gate,menu_parse,pending}.py。
替身：FakeJev、FakeClock、FakeTrace 都来自 tests/fakes（FakeTrace 是 D、E、G 共用的内存 trace，缺口 G-G-01）。
策略一律按 Policy.load(registry.policy_raw()) 组装（缺口 G-G-02）。

本文件不含端口、主机、URL 或密钥；skills 目录来自 conftest 的 skills_dir fixture，临时副本放 tmp_path。
所有构造函数都在测试体里调用（不做成 fixture），这样骨架未实现时测试记为 FAILED（NotImplementedError），
而不是 setup ERROR。
"""

from __future__ import annotations

import copy
import hashlib
import itertools
import os
import re
import shutil
import subprocess
from collections.abc import Callable, Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml
from pydantic import ConfigDict

from fakes import FakeTrace, jev_choice, jev_noul, jev_score
from waitless import models as m
from waitless.decision.gate import DecisionGate
from waitless.decision.policy import Policy
from waitless.decision.registry import load_registry
from waitless.skills.compiler import compile_call
from waitless.skills.loader import load_skills

# ---------------------------------------------------------------------------
# 规格常量（tech §6.6、§6.9）
# ---------------------------------------------------------------------------

SKILL_NAMES: tuple[str, ...] = (
    "assisted-call",
    "find-official-number",
    "navigate-ivr",
    "decide-option",
    "handoff-to-user",
    "company-lakeshore-energy",
)
COMPANY_ID = "lakeshore-energy"
COMPANY_SKILL = "company-lakeshore-energy"

# §6.9 为每个 skill 列出的结构化文件（AC-39 的机制按这张表逐目录核对）
STRUCTURED_FILES: dict[str, tuple[str, ...]] = {
    "assisted-call": ("jev/questions.yaml", "policy.yaml"),
    "find-official-number": ("references/trusted-sources.yaml",),
    "navigate-ivr": ("references/ivr-rules.yaml",),
    "decide-option": (
        "jev/questions.yaml",
        "policy.yaml",
        "references/intents.yaml",
        "references/lexicons.yaml",
        "references/stall-phrases.yaml",
        "references/say-templates.yaml",
    ),
    "handoff-to-user": ("references/handoff.yaml",),
    "company-lakeshore-energy": ("references/ivr-map.yaml", "references/pre-call-questions.yaml"),
}
# §6.6 决策点注册表的「所在文件」列
POINT_HOME: dict[str, str] = {
    "assistant_next_action": "assisted-call",
    "company_skill": "assisted-call",
    "task_intent": "assisted-call",
    "precall_default": "assisted-call",
    "call_round": "decide-option",
    "said_as_decided": "decide-option",
    "call_outcome": "decide-option",
    "result_item_kind": "decide-option",
}
SINGLE_POINTS: tuple[str, ...] = tuple(p for p in m.DECISION_POINT_CODES if p != "call_round")

# 测试身份（虚构）
CALL_ID = "call-g-0001"
TASK_ID = "task-g-0001"
JEV_MODEL = "test-jev-model"  # 与 conftest.mode_env 的 WAITLESS_JEV_MODEL 相同；不是密钥
ACCOUNT_NUMBER = "20481234"
GOAL = "Start electric service at the new apartment on November 1"
DEPOSIT_LABEL = "Pay a security deposit"
AUTOPAY_LABEL = "Enroll in autopay"

# 对方台词（虚构；词表命中按 tech §6.6 给出的例词）
MENU_TEXT = (
    "Thank you for calling Lakeshore Energy. To start, stop, or move service, press 1. "
    "For billing questions, press 2. To hear these options again, press 9."
)
MENU_OPTIONS: tuple[tuple[str, str], ...] = (
    ("1", "To start, stop, or move service"),
    ("2", "For billing questions"),
    ("9", "To hear these options again"),
)
NEUTRAL_TEXT = "Thanks for holding. How can I help you today?"
ADDRESS_TEXT = "What is the service address for the new account?"
KEYPAD_TEXT = "Please enter your account number, followed by the pound key."
IDENTITY_TEXT = "Thanks. Before we go on, am I speaking with the account holder?"
DOB_TEXT = "To continue, I need to verify the date of birth on the account."
DEPOSIT_TEXT = "Last thing: would you like to pay a deposit, or enroll in autopay instead?"
FEE_TEXT = (
    "We can start service on November 1 for a $25 expedite fee, or on November 3 at no charge. "
    "Which would you prefer?"
)
SURGE_TEXT = "There is also an optional $40 surge protection plan for the meter. Should I add it?"
SURVEY_TEXT = "Before you go, would you be willing to take a short customer satisfaction survey?"

# 缺口 G-G-05：偏好套用用常设规则之外的低后果话题（回电时段）；标签由 G 加进 lexicons.yaml。
CALLBACK_TEXT = "What is the best time for a callback if we get disconnected?"


def topic_labels(compiled: m.CompiledCall, text: str) -> list[str]:
    """本轮文字命中的 lexicons 标签（不含 field_cues.* 这类展平键），即 PriorChoice.question_key 的取法（A-03）。"""
    low = text.lower()
    return sorted(
        key
        for key, words in compiled.lexicons.items()
        if "." not in key and any(w.lower() in low for w in words)
    )


FIELD_PLACEHOLDERS = frozenset(
    {"name", "phone_number", "address", "account_number", "email", "zip", *m.FORBIDDEN_FIELD_KEYS}
)


# ---------------------------------------------------------------------------
# 文件与 YAML
# ---------------------------------------------------------------------------


def require_skills_dir(skills_dir: Path) -> Path:
    if not skills_dir.is_dir():
        raise AssertionError(f"skills directory is missing: {skills_dir} (G lane creates it, tech §6.9)")
    return skills_dir


def copy_skills(skills_dir: Path, tmp_path: Path, name: str = "skills") -> Path:
    """把仓库的 skills/ 复制到 tmp_path/<name>，返回副本根目录（测试只改副本）。"""
    require_skills_dir(skills_dir)
    dest = tmp_path / name
    shutil.copytree(skills_dir, dest)
    return dest


def read_yaml(path: Path) -> Any:
    return yaml.safe_load(path.read_text(encoding="utf-8"))


def write_yaml(path: Path, doc: Any) -> None:
    path.write_text(yaml.safe_dump(doc, sort_keys=False, allow_unicode=True), encoding="utf-8")


def edit_yaml(path: Path, fn: Callable[[Any], Any]) -> None:
    """读出 YAML，交给 fn 原地修改，再写回（fn 的返回值不用）。"""
    doc = read_yaml(path)
    fn(doc)
    write_yaml(path, doc)


def frontmatter(path: Path) -> tuple[dict[str, Any], str]:
    """测试自带的 frontmatter 解析（不经被测加载器）。"""
    text = path.read_text(encoding="utf-8")
    mt = re.match(r"\A---[ \t]*\r?\n(.*?)\r?\n---[ \t]*(?:\r?\n|\Z)(.*)\Z", text, re.S)
    if not mt:
        raise AssertionError(f"{path} has no YAML frontmatter")
    meta = yaml.safe_load(mt.group(1)) or {}
    if not isinstance(meta, dict):
        raise AssertionError(f"{path} frontmatter is not a mapping")
    return meta, mt.group(2)


def write_frontmatter(path: Path, meta: Mapping[str, Any], body: str) -> None:
    path.write_text(
        "---\n" + yaml.safe_dump(dict(meta), sort_keys=False, allow_unicode=True) + "---\n" + body,
        encoding="utf-8",
    )


def all_strings(doc: Any) -> list[str]:
    """YAML 文档里全部字符串值（递归，不含映射的键）。"""
    out: list[str] = []
    if isinstance(doc, str):
        out.append(doc)
    elif isinstance(doc, Mapping):
        for v in doc.values():
            out.extend(all_strings(v))
    elif isinstance(doc, (list, tuple)):
        for v in doc:
            out.extend(all_strings(v))
    return out


def find_key(doc: Any, key: str) -> list[Any]:
    """递归找出所有名为 key 的映射项的值。"""
    hits: list[Any] = []
    if isinstance(doc, Mapping):
        for k, v in doc.items():
            if k == key:
                hits.append(v)
            hits.extend(find_key(v, key))
    elif isinstance(doc, (list, tuple)):
        for v in doc:
            hits.extend(find_key(v, key))
    return hits


def set_key_everywhere(doc: Any, key: str, value: Any) -> int:
    """把文档里所有名为 key 的项改成 value，返回改了几处。"""
    n = 0
    if isinstance(doc, dict):
        for k in list(doc):
            if k == key:
                doc[k] = value
                n += 1
            else:
                n += set_key_everywhere(doc[k], key, value)
    elif isinstance(doc, list):
        for v in doc:
            n += set_key_everywhere(v, key, value)
    return n


def points_index(skills_dir: Path) -> dict[str, list[tuple[str, dict[str, Any]]]]:
    """point_id → [(skill 名, 该点在 jev/questions.yaml 里的文档)]（§6.6 文件结构，测试侧直接读文件）。"""
    out: dict[str, list[tuple[str, dict[str, Any]]]] = {}
    for d in sorted(p for p in skills_dir.iterdir() if p.is_dir()):
        f = d / "jev" / "questions.yaml"
        if not f.is_file():
            continue
        doc = read_yaml(f) or {}
        for point, body in (doc.get("decision_points") or {}).items():
            out.setdefault(point, []).append((d.name, body))
    return out


def policy_index(skills_dir: Path) -> dict[str, list[tuple[str, Any]]]:
    out: dict[str, list[tuple[str, Any]]] = {}
    for d in sorted(p for p in skills_dir.iterdir() if p.is_dir()):
        f = d / "policy.yaml"
        if not f.is_file():
            continue
        doc = read_yaml(f) or {}
        for point, body in (doc.get("decision_points") or {}).items():
            out.setdefault(point, []).append((d.name, body))
    return out


def point_questions(skills_dir: Path, point: str) -> dict[str, Any]:
    homes = points_index(skills_dir).get(point) or []
    if len(homes) != 1:
        raise AssertionError(f"{point} must be defined in exactly one jev/questions.yaml, found {len(homes)}")
    return homes[0][1]["questions"]


def merged_policy_raw(skills_dir: Path) -> dict[str, Any]:
    """测试侧直接读文件得到的合并 policy（version、global、decision_points），用来核对 registry.policy_raw()。

    按 §6.6 的文件结构合并：global 取 decide-option/policy.yaml；decision_points 取各 skill 的并集（G-G-02 的定义）。
    被测代码的入口是 registry.policy_raw()（见 policy_raw()）。
    """
    raw: dict[str, Any] = {"version": 1, "decision_points": {}}
    for name in SKILL_NAMES:
        f = skills_dir / name / "policy.yaml"
        if not f.is_file():
            continue
        doc = read_yaml(f) or {}
        if "version" in doc:
            raw["version"] = doc["version"]
        if "global" in doc:
            raw["global"] = copy.deepcopy(doc["global"])
        raw["decision_points"].update(copy.deepcopy(doc.get("decision_points") or {}))
    return raw


def policy_raw(skills_dir: Path) -> dict[str, Any]:
    """被测代码给出的合并 policy：load_registry(load_skills(dir)).validate().policy_raw()（G-G-02）。"""
    return load_registry(load_skills(require_skills_dir(skills_dir))).validate().policy_raw()


def loaded_policy(skills_dir: Path) -> Policy:
    return Policy.load(policy_raw(skills_dir))


def ivr_rules(skills_dir: Path) -> Any:
    return read_yaml(skills_dir / "navigate-ivr" / "references" / "ivr-rules.yaml")


def press_patterns(skills_dir: Path) -> list[str]:
    """navigate-ivr references/ivr-rules.yaml 里的 press_patterns（按键名找，不依赖嵌套层级）。"""
    hits = find_key(read_yaml(skills_dir / "navigate-ivr" / "references" / "ivr-rules.yaml"), "press_patterns")
    if not hits or not isinstance(hits[0], list) or not hits[0]:
        raise AssertionError("navigate-ivr/references/ivr-rules.yaml needs a non-empty press_patterns list")
    return [str(p) for p in hits[0]]


def tree_sha(root: Path) -> str:
    """测试自带的目录哈希：按相对路径排序，逐个哈希（路径 + 内容）。"""
    h = hashlib.sha256()
    for p in sorted(x for x in root.rglob("*") if x.is_file()):
        h.update(p.relative_to(root).as_posix().encode())
        h.update(b"\0")
        h.update(p.read_bytes())
        h.update(b"\0")
    return h.hexdigest()


def strip_none(obj: Any) -> Any:
    if isinstance(obj, Mapping):
        return {k: strip_none(v) for k, v in obj.items() if v is not None}
    if isinstance(obj, list):
        return [strip_none(v) for v in obj]
    return obj


# ---------------------------------------------------------------------------
# git（AC-40）
# ---------------------------------------------------------------------------


def isolate_git(monkeypatch: Any, tmp_path: Path) -> None:
    """不让 git 往 tmp_path 外面找仓库，也不读用户的全局配置。"""
    monkeypatch.setenv("GIT_CEILING_DIRECTORIES", str(tmp_path))
    monkeypatch.setenv("GIT_CONFIG_NOSYSTEM", "1")
    monkeypatch.setenv("GIT_CONFIG_GLOBAL", os.devnull)
    for name in ("GIT_DIR", "GIT_WORK_TREE", "GIT_INDEX_FILE", "GIT_OBJECT_DIRECTORY"):
        monkeypatch.delenv(name, raising=False)


def git(repo: Path, *args: str) -> str:
    exe = shutil.which("git")
    if exe is None:
        raise AssertionError("the git executable is required for the AC-40 tests")
    env = {
        **os.environ,
        "GIT_AUTHOR_NAME": "Waitless Test",
        "GIT_AUTHOR_EMAIL": "test@example.invalid",
        "GIT_COMMITTER_NAME": "Waitless Test",
        "GIT_COMMITTER_EMAIL": "test@example.invalid",
    }
    done = subprocess.run(
        [exe, "-c", "commit.gpgsign=false", "-c", "init.defaultBranch=main", *args],
        cwd=repo,
        env=env,
        check=True,
        capture_output=True,
        text=True,
    )
    return done.stdout.strip()


def git_repo_with_skills(skills_dir: Path, tmp_path: Path) -> tuple[Path, Path]:
    """tmp_path/repo 是新建的 git 仓库，skills/ 是仓库 skills 的副本，已提交一次。"""
    repo = tmp_path / "repo"
    repo.mkdir()
    root = copy_skills(skills_dir, repo)
    git(repo, "init", "-q")
    git(repo, "add", "-A")
    git(repo, "commit", "-q", "-m", "skills")
    return repo, root


def last_commit(repo: Path, rel: str) -> str:
    return git(repo, "log", "-1", "--format=%H", "--", rel)


# ---------------------------------------------------------------------------
# 数据构造
# ---------------------------------------------------------------------------


def option(oid: str, label: str, detail: str) -> m.Option:
    return m.Option(id=oid, label=label, detail=detail)


def task_card(
    *,
    selected: str | None = "deposit",
    precall: bool = True,
    goal: str = GOAL,
    company_id: str = COMPANY_ID,
    card_version: int = 1,
) -> m.TaskCard:
    decisions: list[m.PrecallDecision] = []
    if precall:
        decisions.append(
            m.PrecallDecision(
                question_id="deposit_or_autopay",
                question="Pay a deposit or enroll in autopay?",
                options=[
                    option("deposit", DEPOSIT_LABEL, "Refundable deposit, billed once"),
                    option("autopay", AUTOPAY_LABEL, "Monthly bill paid automatically"),
                ],
                default="deposit",
                selected=selected,
                needs_user=selected is None,
            )
        )
    return m.TaskCard(
        card_version=card_version,
        company=m.TaskCompany(
            company_id=company_id,
            name="Lakeshore Energy",
            official_number="(312) 555-0100",
            sources=[
                m.Source(url="lakeshore-energy.example/contact.html", title="Contact"),
                m.Source(url="lakeshore-energy.example/start-service.html", title="Start service"),
            ],
        ),
        goal=goal,
        task_summary="Start electricity service on November 1 at the new apartment",
        effective_date="2026-11-01",
        shared_fields=[
            m.SharedField(key="name", label="Name", display="Alex Example"),
            m.SharedField(key="address", label="Service address", display="1200 W Example St, Apt 4B"),
        ],
        precall_decisions=decisions,
        boundary="Only start service; no other account changes.",
    )


def profile(base: Mapping[str, str], *, account: str | None = ACCOUNT_NUMBER, **over: Any) -> m.Profile:
    data: dict[str, Any] = {**base, **over}
    if account is not None:
        data["accounts"] = {COMPANY_ID: account}
    return m.Profile(**data)


class DecoyProfile(m.Profile):
    """带诱饵敏感键值的档案（AC-42、AC-107）。

    Profile 本身拒绝未声明的键；这里放开 extra，模拟「档案里混进了 ssn、出生日期、卡号」的情形。
    isinstance(DecoyProfile(...), Profile) 为真，可以直接传给 compile_call。
    """

    model_config = ConfigDict(extra="allow")


def decoy_profile(
    base: Mapping[str, str], decoys: Mapping[str, str], *, account: str | None = ACCOUNT_NUMBER
) -> DecoyProfile:
    data: dict[str, Any] = {**base, **decoys}
    if account is not None:
        data["accounts"] = {COMPANY_ID: account}
    p = DecoyProfile(**data)
    dumped = p.model_dump()
    for k, v in decoys.items():
        assert dumped.get(k) == v, "decoy value must really be present on the profile"
    return p


def standing_rules() -> list[m.StandingRule]:
    """R47 的 3 条常设规则（A 线负责初始化写库；这里只构造同样的数据）。"""
    return [
        m.StandingRule(rule_key="decline_surveys", rule_text="Decline surveys and marketing offers.", scope="all"),
        m.StandingRule(rule_key="paperless", rule_text="Choose paperless billing when asked.", scope="all"),
        m.StandingRule(rule_key="language", rule_text="Choose English when asked for a language.", scope="all"),
    ]


def speech_turn(text: str, *, seq: int = 5) -> m.CompanyTurn:
    return m.CompanyTurn(
        call_id=CALL_ID, seq=seq, t_start_ms=1000 * seq, t_end_ms=1000 * seq + 900, text=text, kind="speech"
    )


def menu_turn(
    text: str = MENU_TEXT, options: Sequence[tuple[str, str]] = MENU_OPTIONS, *, seq: int = 1
) -> m.CompanyTurn:
    return m.CompanyTurn(
        call_id=CALL_ID,
        seq=seq,
        t_start_ms=1000 * seq,
        t_end_ms=1000 * seq + 4000,
        text=text,
        kind="menu",
        menu_options=[m.MenuOption(digits=d, label=label) for d, label in options],
    )


def line(speaker: str, text: str) -> m.TranscriptLine:
    return m.TranscriptLine(speaker=speaker, text=text)  # type: ignore[arg-type]


def call_transcript(turn_text: str, *, earlier: int = 2) -> list[m.TranscriptLine]:
    """本通最近的转写，最后一行是本轮（RoundContext.transcript，含本轮）。"""
    pool = [
        line("company", "Thank you for calling Lakeshore Energy."),
        line("waitless", "Hi, I'm an AI assistant calling on behalf of a customer."),
        line("company", "Sure, one moment please."),
        line("waitless", "Thank you."),
    ]
    lines: list[m.TranscriptLine] = []
    for i in range(earlier):
        lines.append(pool[i % len(pool)].model_copy(update={"text": f"{pool[i % len(pool)].text} ({i})"}))
    lines.append(line("company", turn_text))
    return lines


def tool_candidates() -> list[m.Candidate]:
    """DP-A1 的候选：请求里带来的 tools/list（§6.2）。描述是测试文字。"""
    return [m.Candidate(id=n, label=n, description=f"MCP tool {n}") for n in m.TOOL_NAMES]


# ---------------------------------------------------------------------------
# Jev 应答脚本
# ---------------------------------------------------------------------------


class PointChooser:
    """FakeJev 的可调用脚本条目：对每道 choice 题按 select(criteria) 选一项，noul/score 给固定值。

    不依赖题目 id，只依赖题型与 criteria；chosen 记下每道 choice 题选了什么、criteria 是什么。
    """

    def __init__(
        self,
        select: Callable[[list[str]], str],
        *,
        confidence: float = 0.9,
        noul: float = 0.05,
        score: float = 0.0,
    ) -> None:
        self.select = select
        self.confidence = confidence
        self.noul = noul
        self.score = score
        self.chosen: dict[str, str] = {}
        self.criteria: dict[str, list[str]] = {}

    def __call__(self, state: Any, qs: Mapping[str, m.JevQuestion], point: Any) -> dict[str, Any]:
        out: dict[str, Any] = {}
        for qid, q in qs.items():
            if q.type == "choice":
                crit = list(q.criteria or {})
                pick = self.select(crit)
                self.chosen[qid] = pick
                self.criteria[qid] = crit
                out[qid] = jev_choice(pick, self.confidence)
            elif q.type == "noul":
                out[qid] = jev_noul(self.noul)
            else:
                out[qid] = jev_score(self.score, 0.9)
        return out

    @property
    def only_choice(self) -> str:
        assert len(self.chosen) == 1, f"expected one choice question, got {sorted(self.chosen)}"
        return next(iter(self.chosen.values()))


def prefer(*ids: str, exclude: Iterable[str] = ()) -> Callable[[list[str]], str]:
    """先选 ids 里第一个出现在 criteria 中的；都没有时选第一个不在 exclude 里的。"""
    banned = set(exclude)

    def select(crit: list[str]) -> str:
        for i in ids:
            if i in crit:
                return i
        for c in crit:
            if c not in banned:
                return c
        raise AssertionError(f"no selectable criterion in {crit}")

    return select


def round_pick(
    prefer_ids: Sequence[str],
    *,
    fallback: str = "open_answer",
    menu_confidence: float = 0.9,
    call_phase: str = "live_rep",
    needs_user: float = 0.05,
    stakes: float = 0.0,
) -> Callable[..., dict[str, Any]]:
    """call_round 的可调用条目：menu_choice 取 prefer_ids 里第一个出现在候选中的，否则取 fallback。"""
    chosen: dict[str, Any] = {}

    def entry(state: Any, qs: Mapping[str, m.JevQuestion], point: Any) -> dict[str, Any]:
        crit = list(qs["menu_choice"].criteria or {})
        pick = next((c for c in prefer_ids if c in crit), fallback)
        chosen["menu_choice"] = pick
        chosen["criteria"] = crit
        return {
            "menu_choice": jev_choice(pick, menu_confidence),
            "call_phase": jev_choice(call_phase, 0.9),
            "needs_user": jev_noul(needs_user),
            "stakes": jev_score(stakes, 0.9),
        }

    entry.chosen = chosen  # type: ignore[attr-defined]
    return entry


def choice_answer(choice: str, confidence: float, others: Sequence[str]) -> m.JevAnswer:
    rest = [o for o in others if o != choice]
    probs = {choice: confidence}
    if rest:
        share = (1.0 - confidence) / len(rest)
        probs.update({o: share for o in rest})
    return m.JevAnswer(type="choice", choice=choice, confidence=confidence, probabilities=probs)


def round_jev_answers(
    candidate_ids: Sequence[str],
    menu_choice: str,
    *,
    menu_confidence: float = 0.9,
    call_phase: str = "live_rep",
    needs_user: float = 0.05,
    stakes: float = 0.0,
) -> dict[str, m.JevAnswer]:
    """直接喂给 Policy.evaluate_round 的 4 题答案（不经 FakeJev）。"""
    phases = ["ivr_menu", "hold", "ai_agent", "live_rep", "identity_check", "ended"]
    return {
        "menu_choice": choice_answer(menu_choice, menu_confidence, list(candidate_ids)),
        "call_phase": choice_answer(call_phase, 0.9, phases),
        "needs_user": m.JevAnswer(type="noul", noul=needs_user),
        "stakes": m.JevAnswer(type="score", score=stakes, confidence=0.9),
    }


# ---------------------------------------------------------------------------
# 决策门装配
# ---------------------------------------------------------------------------


@dataclass
class Kit:
    skills_dir: Path
    skills: dict[str, m.Skill]
    registry: Any
    policy: Any
    gate: Any
    trace: FakeTrace
    jev: Any
    clock: Any
    compiled: m.CompiledCall
    rules: list[m.StandingRule]
    _req_ids: Any = field(default_factory=lambda: itertools.count(1))

    def context(
        self,
        *,
        phase: str = "live_rep",
        interrupts_used: int = 0,
        prior_choices: Sequence[m.PriorChoice] = (),
        with_card: bool = True,
        allowed_fields: Mapping[str, str] | None = None,
    ) -> m.DecisionContext:
        return m.DecisionContext(
            task_card=self.compiled.task_card if with_card else None,
            allowed_fields=dict(self.compiled.allowed_fields if allowed_fields is None else allowed_fields),
            standing_rules=list(self.rules),
            prior_choices=list(prior_choices),
            interrupts_used=interrupts_used,
            phase=phase,  # type: ignore[arg-type]
        )

    def round_ctx(
        self,
        turn: m.CompanyTurn,
        *,
        phase: str = "live_rep",
        interrupts_used: int = 0,
        prior_choices: Sequence[m.PriorChoice] = (),
        transcript: Sequence[m.TranscriptLine] | None = None,
    ) -> m.RoundContext:
        return m.RoundContext(
            call_id=CALL_ID,
            task_id=TASK_ID,
            compiled=self.compiled,
            context=self.context(phase=phase, interrupts_used=interrupts_used, prior_choices=prior_choices),
            transcript=list(call_transcript(turn.text) if transcript is None else transcript),
        )

    async def round(self, turn: m.CompanyTurn, **kw: Any) -> m.DecisionResult:
        return await self.gate.decide_round(turn, self.round_ctx(turn, **kw))

    def request(
        self,
        point: str,
        *,
        candidates: Sequence[m.Candidate] = (),
        transcript: Sequence[m.TranscriptLine] = (),
        template_vars: Mapping[str, str] | None = None,
        call_id: str | None = None,
        phase: str = "idle",
        with_card: bool = False,
        interrupts_used: int = 0,
        turn: m.CompanyTurn | None = None,
        attempt: int | None = None,
    ) -> m.DecisionRequest:
        extra: dict[str, Any] = {}
        if attempt is not None:
            extra["attempt"] = attempt  # 歧义定稿 A-02：DP-C2 的核对次数（待骨架 r2 加字段）
        return m.DecisionRequest(
            decision_id=f"req-{next(self._req_ids)}",
            point=point,  # type: ignore[arg-type]
            call_id=call_id,
            task_id=TASK_ID,
            turn=turn,
            candidates=list(candidates),
            context=self.context(phase=phase, interrupts_used=interrupts_used, with_card=with_card),
            transcript=list(transcript),
            template_vars=dict(template_vars or {}),
            **extra,
        )

    async def decide(self, point: str, **kw: Any) -> m.DecisionResult:
        return await self.gate.decide(point, self.request(point, **kw))

    def round_request(
        self,
        turn: m.CompanyTurn,
        *,
        phase: str = "live_rep",
        interrupts_used: int = 0,
        prior_choices: Sequence[m.PriorChoice] = (),
    ) -> m.DecisionRequest:
        """给 Policy.evaluate_round 直接用的 call_round 请求（候选取自 round_candidates）。"""
        from waitless.decision.gate import round_candidates

        return m.DecisionRequest(
            decision_id=f"req-{next(self._req_ids)}",
            point="call_round",
            call_id=CALL_ID,
            task_id=TASK_ID,
            turn=turn,
            candidates=round_candidates(turn, self.compiled),
            context=self.context(phase=phase, interrupts_used=interrupts_used, prior_choices=prior_choices),
            transcript=call_transcript(turn.text),
        )


def build_kit(
    skills_dir: Path,
    *,
    jev: Any,
    clock: Any,
    prof: m.Profile,
    card: m.TaskCard | None = None,
    rules: Sequence[m.StandingRule] | None = None,
    prefs: Sequence[m.Preference] = (),
) -> Kit:
    """load_skills → load_registry().validate() → Policy.load(registry.policy_raw()) → compile_call → DecisionGate。"""
    require_skills_dir(skills_dir)
    skills = load_skills(skills_dir)
    registry = load_registry(skills).validate()
    policy = Policy.load(registry.policy_raw())
    trace = FakeTrace(clock)
    rules_list = list(standing_rules() if rules is None else rules)
    compiled = compile_call(card or task_card(), skills, prof, rules_list, list(prefs))
    ids = itertools.count(1)
    gate = DecisionGate(
        jev=jev,
        jev_model=JEV_MODEL,
        registry=registry,
        policy=policy,
        trace=trace,
        clock=clock,
        ids=lambda: f"dec-{next(ids)}",
    )
    return Kit(
        skills_dir=skills_dir,
        skills=skills,
        registry=registry,
        policy=policy,
        gate=gate,
        trace=trace,
        jev=jev,
        clock=clock,
        compiled=compiled,
        rules=rules_list,
    )


def assert_no_field_values(text: str | None, kit: Kit, extra: Iterable[str] = ()) -> None:
    """注入话术里不含任何字段值（AC-52、AC-107）。"""
    if not text:
        return
    for value in [*kit.compiled.allowed_fields.values(), *extra]:
        if value:
            assert value not in text, f"utterance leaks a field value {value!r}: {text!r}"


def placeholders(template: str) -> set[str]:
    return {mt.group(1) for mt in re.finditer(r"\{([A-Za-z_][A-Za-z0-9_]*)", template)}
