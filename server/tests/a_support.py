"""A 线（后端核心、助手与 MCP）测试共用的替身与夹具（第 3 步写，冻结：各施工线不改）。

只经骨架的公开名字（models、interfaces、§7 表里的类与函数）和 tech §6 的接口访问被测代码。
这里的替身只替代「别的线」或外部服务：
  ScriptedGate      G 线的 DecisionGate（按脚本给出策略之后的 DecisionResult，并像真决策门一样先写 decision 事件）
  RecordingLlm      LlmTasks（L1–L5），记录入参、按脚本返回
  （内存 trace 用共用替身 fakes.FakeTrace，r3 缺口 G-G-01）
  FakeSession / FakeSessionFactory  D、E 线的 CallSession
  FakeLineTokens / FakeHandoffTokens / FakeCallRegistry / FakePending  D、G 线的小接口
  StubHandlers      mcp_server.ToolHandlers 的替身：9 个方法返回预制的 *Out 模型（只测 MCP 注册与传输层）
  DictFetcher       official_number.Fetcher：URL → 页面文字
夹具：
  make_settings()   直接构造 config.Settings（定稿 A-27；不经 load_settings，免得非配置测试依赖配置代码）
  write_web()       网页夹具（index.html、js/*.mjs）
  build_skills()    在临时目录写出 assisted-call、find-official-number 与一个公司 skill，返回 name → Skill
  canned_outputs()  9 个工具的合法输出样例
  mcp_session()     官方 MCP Python SDK 客户端（mcp==2.3.0，Streamable HTTP，2025-11-25 握手）经 ASGI 连 app 的 /mcp

技能夹具里 ivr-map.yaml、pre-call-questions.yaml、trusted-sources.yaml 按 r3 冻结的 models.IvrMap、PrecallQuestionFile、
TrustedSources 写（缺口 A-G-4、A-G-5：required_fields 在每个意图里），build_skills 写出前先用这些模型校验；
assisted-call 的 jev/questions.yaml 只是 G 线文件的示意（A 线代码不读它）。
所有值都是测试值，不是密钥；不写死端口，主机只用回环测试值。
"""

from __future__ import annotations

import hashlib
import itertools
import json
from collections.abc import Callable, Iterable, Mapping, Sequence
from contextlib import AsyncExitStack, asynccontextmanager
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

import yaml

from waitless import models as m
from waitless.config import Settings  # 定稿 A-27：Settings 归 config.py（A 线）；EngineConfig 仍在 models
from waitless.interfaces import AppServices, CallDeps, ToolFailure
from waitless.mcp_server import ToolHandlers

from fakes import FakeClock

SERVER_DIR = Path(__file__).resolve().parents[1]
ENGINE_YAML = SERVER_DIR / "config" / "engine.yaml"

NOW = datetime(2026, 10, 14, 12, 0, tzinfo=timezone.utc)
COMMIT_A = "a" * 40
COMMIT_B = "b" * 40
SHA = "c" * 64

LOOPBACK = "127.0.0.1"  # 测试值：只用回环
BASE_URL = f"http://{LOOPBACK}"  # ASGI 进程内传输用的基址，不开端口

# 测试档案（虚构）；与 conftest.TEST_PROFILE 一致，另加账户号
PROFILE_VALUES: dict[str, str] = {
    "name": "Alex Example",
    "phone_number": "(312) 555-0147",
    "address": "1200 W Example St, Apt 4B",
    "account_number": "8800123456",
}

LAKESHORE_NUMBER = "(312) 555-0100"
LAKESHORE_DOMAIN = "lakeshore-energy.example"
LAKESHORE_URLS = (
    f"https://{LAKESHORE_DOMAIN}/contact.html",
    f"https://{LAKESHORE_DOMAIN}/start-service.html",
)
ASSISTED_BODY = (
    "Assisted-call fixture body (fixture-body-4c1d). Speak in one or two short, calm sentences. "
    "Never start a call yourself; the user clicks Start."
)

JEV_QID: dict[str, str] = {
    "assistant_next_action": "next_action",
    "company_skill": "company",
    "task_intent": "intent",
    "precall_default": "precall",
    "said_as_decided": "said",
    "call_outcome": "outcome",
    "result_item_kind": "kind",
}


# ---------------------------------------------------------------------------
# Settings 与网页夹具
# ---------------------------------------------------------------------------


def engine_config() -> m.EngineConfig:
    return m.EngineConfig.model_validate(yaml.safe_load(ENGINE_YAML.read_text(encoding="utf-8")))


def make_dirs(root: Path) -> dict[str, Path]:
    dirs = {name: root / name for name in ("web", "skills", "company", "replays")}
    for d in dirs.values():
        d.mkdir(parents=True, exist_ok=True)
    return dirs


def make_settings(root: Path, *, mode: str = "replay", allowed_hosts: Sequence[str] = (LOOPBACK, "localhost"),
                  **over: Any) -> Settings:
    dirs = make_dirs(root)
    data: dict[str, Any] = {
        "mode": mode,
        "host": LOOPBACK,
        "port": 0,
        "allowed_hosts": tuple(allowed_hosts),
        "db_path": root / "waitless.db",
        "web_dir": dirs["web"],
        "skills_dir": dirs["skills"],
        "company_dir": dirs["company"],
        "engine_config_path": ENGINE_YAML,
        "replay_dir": dirs["replays"] if mode == "replay" else None,
        "engine": engine_config(),
    }
    if mode in ("text-sim", "live-sim"):
        data.update(
            aws_region="us-east-1",
            aws_access_key_id="test-not-a-key",
            aws_secret_access_key="test-not-a-secret",
            planner_model_id="test-planner-model",
            jev_url="http://jev.invalid/v1/systemone",
            jev_model="test-jev-model",
            jev_api_key="test-not-a-key",
        )
    if mode == "live-sim":
        data.update(
            carrier_url="http://carrier.invalid",
            line_ws_url="ws://engine.invalid/ws/line",
            sonic_model_id="test-sonic-model",
            sonic_voice="test-voice",
        )
    data.update(over)
    return Settings.model_validate(data)


WEB_INDEX = "<!doctype html>\n<html><head><title>Waitless test page</title></head>\n<body><p>marker-index-7f3a</p></body></html>\n"
WEB_APP_MJS = "export const marker = 'marker-app-91c2';\n"
WEB_WORKLET_JS = "// marker-worklet-55d0\nclass P {}\n"


def write_web(web_dir: Path) -> dict[str, str]:
    (web_dir / "js").mkdir(parents=True, exist_ok=True)
    files = {"index.html": WEB_INDEX, "js/app.mjs": WEB_APP_MJS, "js/pcm-worklet.js": WEB_WORKLET_JS}
    for rel, text in files.items():
        (web_dir / rel).write_text(text, encoding="utf-8")
    return files


# ---------------------------------------------------------------------------
# 技能夹具（tech §6.9 的暂定读法）
# ---------------------------------------------------------------------------


def _dump(value: Any) -> str:
    return value if isinstance(value, str) else yaml.safe_dump(value, sort_keys=False, allow_unicode=True)


def write_skill(root: Path, name: str, *, description: str, body: str, files: Mapping[str, Any],
                metadata: Mapping[str, Any] | None = None, commit: str = COMMIT_A) -> m.Skill:
    """写出一个 skill 目录并返回对应的 models.Skill（不经 G 线的 load_skills）。"""
    d = root / name
    d.mkdir(parents=True, exist_ok=True)
    front: dict[str, Any] = {"name": name, "description": description}
    if metadata:
        front["metadata"] = dict(metadata)
    (d / "SKILL.md").write_text("---\n" + yaml.safe_dump(front, sort_keys=False) + "---\n" + body + "\n", encoding="utf-8")
    for rel, value in files.items():
        p = d / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(_dump(value), encoding="utf-8")
    rels = sorted(str(p.relative_to(d)) for p in d.rglob("*") if p.is_file())
    h = hashlib.sha256()
    for rel in rels:
        h.update(rel.encode())
        h.update((d / rel).read_bytes())
    return m.Skill(
        name=name,
        description=description,
        metadata=dict(metadata or {}),
        body=body,
        path=d,
        files=rels,
        version=m.SkillVersion(commit=commit, content_sha256=h.hexdigest(), dirty=False),
    )


def assisted_call_questions() -> dict[str, Any]:
    """skills/assisted-call/jev/questions.yaml（§6.6 DP-A1、P1–P3）。"""
    return {
        "version": 1,
        "decision_points": {
            "assistant_next_action": {
                "when": "after each user message and each MCP result",
                "questions": {
                    "next_action": {
                        "type": "choice",
                        "instructions": "Pick the single next step of the assisted-call process (fixture-instr-a1).",
                        "criteria_from": "tools_list",
                        "extra_criteria": {
                            "reply_only": "Only reply to the user (fixture)",
                            "ask_clarify": "Ask the user to clarify (fixture)",
                            "present_task_card": "Show the task card and wait for the user's click (fixture)",
                            "wait_for_call": "Keep checking a running call (fixture)",
                        },
                    }
                },
            },
            "company_skill": {
                "when": "inside prepare_task",
                "questions": {
                    "company": {
                        "type": "choice",
                        "instructions": "Which company does the user want Waitless to call? (fixture-instr-p1)",
                        "criteria_from": "company_skills",
                        "extra_criteria": {"none_of_the_above": "No supported company (fixture)"},
                    }
                },
            },
            "task_intent": {
                "when": "after company_skill",
                "questions": {
                    "intent": {
                        "type": "choice",
                        "instructions": "What does the user want done with this company? (fixture-instr-p2)",
                        "criteria_from": "company_intents",
                        "extra_criteria": {"none_of_the_above": "None of these (fixture)"},
                    }
                },
            },
            "precall_default": {
                "when": "for each pre-call question",
                "questions": {
                    "precall": {
                        "type": "choice",
                        "instructions": "Did the user's own words already choose an option? (fixture-instr-p3)",
                        "criteria_from": "precall_options",
                        "extra_criteria": {"keep_default": "Keep the default", "ask_user": "Ask the user"},
                    }
                },
            },
        },
    }


def company_skill_files(*, company_id: str, company_name: str, pattern: str,
                        intents: Sequence[Mapping[str, Any]], questions: Sequence[Mapping[str, Any]],
                        keypad_fields: Sequence[str] = ("account_number",)) -> dict[str, Any]:
    """公司 skill 的两个 references 文件；先按 r3 冻结模型校验（IvrMap 没有 company_id 字段，
    required_fields 在每个意图里，A-G-4、A-G-5）。"""
    ivr_map = {
        "version": 1,
        "company_name": company_name,
        "confirmation_pattern": pattern,
        "keypad_fields": list(keypad_fields),
        "intents": [dict(i) for i in intents],
    }
    precall = {"version": 1, "questions": [dict(q) for q in questions]}
    m.IvrMap.model_validate(ivr_map)
    m.PrecallQuestionFile.model_validate(precall)
    return {
        "references/ivr-map.yaml": ivr_map,
        "references/pre-call-questions.yaml": precall,
        "evals/cases.jsonl": json.dumps({"case_id": f"{company_id}-1", "skill": f"company-{company_id}",
                                         "kind": "planning", "input": {}, "expect": {}}) + "\n",
    }


LAKESHORE_INTENTS: tuple[dict[str, Any], ...] = (
    {
        "id": "start_service",
        "description": "Start electricity service at a new address",
        "goal": "Start electricity service at {service_address} on {effective_date}",
        "task_summary": "Ask Lakeshore Energy to start power at {service_address} beginning {effective_date}.",
        "required_fields": ["name", "phone_number", "address", "account_number"],
        "menu_path": ["1", "1"],
    },
    {
        "id": "stop_service",
        "description": "Stop electricity service at the current address",
        "goal": "Stop electricity service at {service_address} on {effective_date}",
        "task_summary": "Ask Lakeshore Energy to shut off power at {service_address} on {effective_date}.",
        "required_fields": ["name", "account_number"],
        "menu_path": ["1", "2"],
    },
)

LAKESHORE_QUESTIONS: tuple[dict[str, Any], ...] = (
    {
        "question_id": "deposit_or_autopay",
        "question": "New service may need a deposit. Pay a deposit or enroll in autopay?",
        "options": [
            {"id": "deposit", "label": "Pay a deposit", "detail": "One-time refundable deposit"},
            {"id": "autopay", "label": "Enroll in autopay", "detail": "Waives the deposit"},
        ],
        "default": "deposit",
        "stakes": 3,
        "tags": ["deposit", "autopay"],
    },
    {
        "question_id": "bill_delivery",
        "question": "How should bills be delivered?",
        "options": [
            {"id": "paperless", "label": "Paperless", "detail": "Email bills"},
            {"id": "paper", "label": "Paper", "detail": "Mailed bills"},
        ],
        "default": "paperless",
        "stakes": 1,
        "tags": ["paperless", "paper bill"],
    },
)


def trusted_sources(companies: Mapping[str, Mapping[str, Any]], *, min_agreeing: int = 2) -> dict[str, Any]:
    """find-official-number/references/trusted-sources.yaml（§6.9：companies.<id>.official_domains、sources[]、
    min_agreeing_sources）。"""
    data = {"version": 1, "min_agreeing_sources": min_agreeing, "companies": {k: dict(v) for k, v in companies.items()}}
    m.TrustedSources.model_validate(data)
    return data


LAKESHORE_TRUSTED = {
    "lakeshore-energy": {
        "name": "Lakeshore Energy",
        "official_domains": [LAKESHORE_DOMAIN],
        "sources": [
            {"url": LAKESHORE_URLS[0], "title": "Contact Lakeshore Energy"},
            {"url": LAKESHORE_URLS[1], "title": "Start service"},
        ],
    }
}


def build_skills(root: Path, *, extra_companies: Iterable[Mapping[str, Any]] = ()) -> dict[str, m.Skill]:
    """assisted-call、find-official-number、company-lakeshore-energy（以及 extra_companies 给出的公司 skill）。"""
    root.mkdir(parents=True, exist_ok=True)
    companies = dict(LAKESHORE_TRUSTED)
    skills: dict[str, m.Skill] = {}
    for extra in extra_companies:
        companies[extra["company_id"]] = extra["trusted"]
    skills["assisted-call"] = write_skill(
        root, "assisted-call", description="How any MCP client uses Waitless (fixture).", body=ASSISTED_BODY,
        files={"jev/questions.yaml": assisted_call_questions(),
               "policy.yaml": {"version": 1, "decision_points": {}},
               "evals/cases.jsonl": '{"case_id": "ac-1", "skill": "assisted-call", "kind": "assistant", "input": {}, "expect": {}}\n'},
        metadata={m.SKILL_KIND_KEY: "client"}, commit=COMMIT_A,
    )
    skills["find-official-number"] = write_skill(
        root, "find-official-number", description="Find a company's official number (fixture).",
        body="Only trust official domains.",
        files={"references/trusted-sources.yaml": trusted_sources(companies),
               "evals/cases.jsonl": '{"case_id": "fon-1", "skill": "find-official-number", "kind": "official_number", "input": {}, "expect": {}}\n'},
        metadata={m.SKILL_KIND_KEY: "harness"}, commit=COMMIT_A,
    )
    skills["company-lakeshore-energy"] = write_skill(
        root, "company-lakeshore-energy", description="Lakeshore Energy electricity service (fixture).",
        body="Company knowledge for Lakeshore Energy.",
        files=company_skill_files(company_id="lakeshore-energy", company_name="Lakeshore Energy",
                                  pattern=r"^LE-\d{5}$", intents=LAKESHORE_INTENTS, questions=LAKESHORE_QUESTIONS),
        metadata={m.SKILL_KIND_KEY: "company", m.SKILL_COMPANY_ID_KEY: "lakeshore-energy"}, commit=COMMIT_B,
    )
    for extra in extra_companies:
        name = f"company-{extra['company_id']}"
        skills[name] = write_skill(
            root, name, description=extra["description"], body=f"Company knowledge for {extra['company_name']}.",
            files=company_skill_files(company_id=extra["company_id"], company_name=extra["company_name"],
                                      pattern=extra["pattern"], intents=extra["intents"], questions=extra["questions"]),
            metadata={m.SKILL_KIND_KEY: "company", m.SKILL_COMPANY_ID_KEY: extra["company_id"]}, commit=COMMIT_B,
        )
    return skills


def read_yaml(skill: m.Skill, rel: str) -> Any:
    return yaml.safe_load((skill.path / rel).read_text(encoding="utf-8"))


class DictFetcher:
    """official_number.Fetcher：URL → 页面文字；没有的 URL 返回 None。记录每次请求。"""

    def __init__(self, pages: Mapping[str, str | None]) -> None:
        self.pages = dict(pages)
        self.requested: list[str] = []

    def fetch(self, url: str) -> str | None:
        self.requested.append(url)
        return self.pages.get(url)


def lakeshore_pages(number: str = LAKESHORE_NUMBER) -> dict[str, str]:
    return {
        LAKESHORE_URLS[0]: f"<html><body><h1>Contact us</h1><p>Customer service: {number}</p></body></html>",
        LAKESHORE_URLS[1]: f"<html><body><p>To start service, call {number} (7am-7pm).</p></body></html>",
    }


# ---------------------------------------------------------------------------
# 决策门替身（G 线的 DecisionGate）
# ---------------------------------------------------------------------------


@dataclass
class Pick:
    """一次脚本化决策：intent 是策略之后的结果（DecisionResult.intent）；choice 是 Jev 原始最高项。"""

    intent: str
    choice: str | None = None
    confidence: float = 0.95
    policy_rule: str = "scripted"
    reason_code: str = "scripted"

    @property
    def raw(self) -> str:
        return self.choice if self.choice is not None else self.intent


PickSpec = Pick | str | Callable[[m.DecisionRequest], "Pick | str"]


def _as_pick(spec: Any, request: m.DecisionRequest) -> Pick:
    if callable(spec) and not isinstance(spec, Pick):
        spec = spec(request)
    if isinstance(spec, str):
        return Pick(spec)
    if not isinstance(spec, Pick):
        raise AssertionError(f"bad ScriptedGate entry {spec!r}")
    return spec


def _spread(choice: str, confidence: float, keys: Sequence[str]) -> dict[str, float]:
    keys = list(dict.fromkeys([*keys, choice]))
    if len(keys) == 1:
        return {choice: 1.0}
    rest = round((1.0 - confidence) / (len(keys) - 1), 6)
    return {k: (confidence if k == choice else rest) for k in keys}


class ScriptedGate:
    """实现 interfaces.DecisionGate 的 decide()。脚本：point → Pick | str | 可调用 | 它们的列表（按调用顺序取，
    最后一项重复使用）。像真决策门一样，返回前先往 trace 写 decision 事件（若给了 trace）。"""

    def __init__(self, script: Mapping[str, PickSpec | Sequence[PickSpec]] | None = None, *, trace: Any = None,
                 jev_model: str = "fake-jev") -> None:
        self._script: dict[str, list[Any]] = {}
        for point, spec in (script or {}).items():
            self.on(point, *(spec if isinstance(spec, (list, tuple)) else [spec]))
        self.trace = trace
        self.jev_model = jev_model
        self.requests: list[tuple[str, m.DecisionRequest]] = []
        self.results: list[m.DecisionResult] = []
        self._ids = itertools.count(1)
        self._used: dict[str, int] = {}

    def on(self, point: str, *specs: PickSpec) -> "ScriptedGate":
        self._script.setdefault(point, []).extend(specs)
        return self

    def requests_for(self, point: str) -> list[m.DecisionRequest]:
        return [r for p, r in self.requests if p == point]

    async def decide(self, point: str, request: m.DecisionRequest) -> m.DecisionResult:
        assert point == request.point, f"decide({point!r}) got request.point={request.point!r}"
        self.requests.append((point, request))
        specs = self._script.get(point)
        if not specs:
            raise AssertionError(f"ScriptedGate: no script for decision point {point!r}")
        i = self._used.get(point, 0)
        self._used[point] = i + 1
        pick = _as_pick(specs[min(i, len(specs) - 1)], request)
        qid = JEV_QID.get(point, point)
        criteria = {c.id: (c.description or c.label or c.id) for c in request.candidates}
        criteria.setdefault(pick.raw, "scripted choice")
        question = m.JevQuestion(type="choice", instructions=f"scripted {point}", criteria=criteria)
        answer = m.JevAnswer(type="choice", choice=pick.raw, confidence=pick.confidence,
                             probabilities=_spread(pick.raw, pick.confidence, list(criteria)))
        state = m.JevState(
            task_goal="scripted", user_profile={}, standing_rules=[], prior_choices=[],
            transcript=m.JevTranscript(note=m.JEV_TRANSCRIPT_NOTE, lines=list(request.transcript)[-m.JEV_TRANSCRIPT_MAX_LINES:]),
        )
        exchange = m.JevExchange(
            request=m.JevRequest(model=self.jev_model, state=state, questions={qid: question}),
            response=m.JevResponse(answers={qid: answer}, model=self.jev_model),
            latency_ms=7,
        )
        decision_id = f"dec-{point}-{next(self._ids)}"
        result = m.DecisionResult(
            decision_id=decision_id, point=point, intent=pick.intent, action="wait",  # A-02：非通话决策点 action=wait
            reason_code=pick.reason_code, policy_rule=pick.policy_rule, jev=exchange,
        )
        if self.trace is not None:
            self.trace.event(m.TraceEvent(
                task_id=request.task_id, call_id=request.call_id, event_type="decision", decision_id=decision_id,
                decision_point=point, jev_request=exchange.request.model_dump(mode="json"),
                jev_response=exchange.response.model_dump(mode="json"),
                policy_decision={"intent": pick.intent, "policy_rule": pick.policy_rule}, latency_ms=7,
            ))
        self.results.append(result)
        return result

    async def decide_round(self, turn: m.CompanyTurn, ctx: m.RoundContext) -> m.DecisionResult:
        raise AssertionError("A 线的被测代码不应调用 decide_round（DP-C1 属于通话引擎）")


def candidate_ids(request: m.DecisionRequest) -> list[str]:
    return [c.id for c in request.candidates]


def request_texts(request: m.DecisionRequest) -> str:
    """DecisionRequest 里能放「用户原话/待分类陈述」的位置（A-01：transcript 与 template_vars）拼起来。"""
    parts = [line.text for line in request.transcript] + list(request.template_vars.values())
    return "\n".join(parts)


def default_prepare_gate(*, company: str = "company-lakeshore-energy", intent: str = "start_service",
                         precall: PickSpec = "keep_default", trace: Any = None) -> ScriptedGate:
    return ScriptedGate({"company_skill": company, "task_intent": intent, "precall_default": precall}, trace=trace)


def precall_by_option(mapping: Mapping[str, Pick | str], default: Pick | str = "keep_default") -> Callable[[m.DecisionRequest], Pick | str]:
    """按候选里出现的 user_chose:<option> 判断是哪一题，返回对应的 Pick。mapping：option_id → Pick。"""

    def pick(request: m.DecisionRequest) -> Pick | str:
        ids = set(candidate_ids(request))
        for option_id, spec in mapping.items():
            if f"user_chose:{option_id}" in ids:
                return spec
        return default

    return pick


# ---------------------------------------------------------------------------
# 大模型任务替身（LlmTasks）
# ---------------------------------------------------------------------------


class RecordingLlm:
    """实现 interfaces.LlmTasks；每个方法记录入参，返回脚本值（可调用则以入参调用）。"""

    def __init__(self, *, extracted: m.ExtractedRequest | Callable[[str], m.ExtractedRequest] | None = None,
                 reply: str = "Okay.", offers: Sequence[m.OfferOption] = (),
                 result_items: m.ResultItems | Callable[[Sequence[m.CallTranscriptLine]], m.ResultItems] | None = None,
                 summary: str = "The call finished.") -> None:
        self.extracted = extracted if extracted is not None else m.ExtractedRequest()
        self.reply = reply
        self.offers = list(offers)
        self.result_items = result_items if result_items is not None else m.ResultItems(confirmation_numbers=[], statements=[])
        self.summary = summary
        self.calls: list[tuple[str, dict[str, Any]]] = []

    def calls_for(self, name: str) -> list[dict[str, Any]]:
        return [kw for n, kw in self.calls if n == name]

    async def extract_request(self, user_text: str) -> m.ExtractedRequest:
        self.calls.append(("extract_request", {"user_text": user_text}))
        return self.extracted(user_text) if callable(self.extracted) else self.extracted

    async def phrase_reply(self, *, skill_body: str, decision: m.AssistantDecision, tool_result: m.ToolResultIn | None,
                           user_text: str | None) -> str:
        self.calls.append(("phrase_reply", {"skill_body": skill_body, "decision": decision, "tool_result": tool_result,
                                            "user_text": user_text}))
        return self.reply

    async def extract_offer_options(self, company_text: str) -> list[m.OfferOption]:
        self.calls.append(("extract_offer_options", {"company_text": company_text}))
        return list(self.offers)

    async def extract_result_items(self, transcript: Sequence[m.CallTranscriptLine]) -> m.ResultItems:
        lines = list(transcript)
        self.calls.append(("extract_result_items", {"transcript": lines}))
        return self.result_items(lines) if callable(self.result_items) else self.result_items

    async def write_summary(self, *, result: m.CaseResultOut, transcript: Sequence[m.CallTranscriptLine]) -> str:
        self.calls.append(("write_summary", {"result": result, "transcript": list(transcript)}))
        return self.summary


# ---------------------------------------------------------------------------
# trace、通话、令牌等小替身
# ---------------------------------------------------------------------------


class FakeLineTokens:
    def __init__(self) -> None:
        self.issued: dict[str, str] = {}
        self._serial = 0

    def issue(self, call_id: str) -> str:
        self._serial += 1
        token = f"line-token-{self._serial}"
        self.issued[token] = call_id
        return token

    def verify(self, token: str) -> str | None:
        return self.issued.get(token)

    def revoke(self, call_id: str) -> None:
        self.issued = {token: cid for token, cid in self.issued.items() if cid != call_id}


class FakeHandoffTokens:
    def __init__(self) -> None:
        self.issued: dict[tuple[str, str], str] = {}
        self._serial = 0

    def issue(self, call_id: str, handoff_id: str) -> tuple[str, str]:
        self._serial += 1
        token = f"handoff-token-{self._serial}"
        self.issued[(handoff_id, token)] = call_id
        return token, (NOW + timedelta(minutes=2)).isoformat()

    def consume(self, handoff_id: str, token: str) -> str | None:
        return self.issued.pop((handoff_id, token), None)

    def revoke(self, call_id: str) -> None:
        self.issued = {key: cid for key, cid in self.issued.items() if cid != call_id}


class FakeCallRegistry:
    def __init__(self) -> None:
        self.sessions: dict[str, Any] = {}

    def get_session(self, call_id: str) -> Any:
        return self.sessions.get(call_id)


class FakePending:
    """PendingRegistry 的最小替身：choices 里放预先建好的 PendingChoice（call_id → 选项），A 线测试只读不建。"""

    def __init__(self, choices: Mapping[str, m.PendingChoice] | None = None) -> None:
        self.choices: dict[str, m.PendingChoice] = dict(choices or {})

    def create(self, **kwargs: Any) -> Any:
        raise AssertionError("A 线测试不应创建待决选项")

    def get(self, choice_id: str) -> m.PendingChoice | None:
        return next((c for c in self.choices.values() if c.choice_id == choice_id), None)

    def current(self, call_id: str) -> m.PendingChoice | None:
        c = self.choices.get(call_id)
        return c if c is not None and c.status == "open" else None

    def answer(self, choice_id: str, option_id: str) -> Any:
        raise ToolFailure("unknown_choice", choice_id)

    def expire_due(self) -> list[Any]:
        return []


class FakeSession:
    """interfaces.CallSession 的替身：记录 start()，状态固定为 dialing。"""

    def __init__(self, spec: m.CallSpec, deps: CallDeps) -> None:
        self.spec = spec
        self.deps = deps
        self.call_id = spec.call_id
        self.task_id = spec.task_id
        self.started = False
        self.answers: list[tuple[str, str]] = []
        self._ended = False

    @property
    def scenario_id(self) -> str | None:
        return self.spec.scenario_id

    async def start(self) -> None:
        self.started = True

    def _pending(self) -> m.PendingChoice | None:
        return self.deps.pending.current(self.call_id)

    def status(self, debug: bool = False) -> m.CallStatusOut:
        pending = self._pending()
        return m.CallStatusOut(task_id=self.task_id, call_id=self.call_id,
                               phase="awaiting_user" if pending else "dialing",
                               status_line="Waiting for you" if pending else "Dialing",
                               transcript_tail=[], pending_choice=pending, handoff=None, interrupts_used=1 if pending else 0,
                               elapsed_s=0.0, ended=self._ended, debug=None)

    async def answer(self, choice_id: str, option_id: str) -> m.AnswerChoiceOut:
        pending = self.deps.pending.get(choice_id)
        if pending is None:
            raise ToolFailure("unknown_choice", choice_id)
        if option_id not in [o.id for o in pending.options]:
            raise ToolFailure("invalid_option", option_id)
        self.answers.append((choice_id, option_id))
        return m.AnswerChoiceOut(task_id=self.task_id, choice_id=choice_id, status="choice_recorded", option_id=option_id)

    async def join(self) -> m.JoinCallOut:
        raise ToolFailure("handoff_not_ready", "not ready")

    async def return_to_agent(self, handoff_id: str) -> m.ReturnOut:
        raise ToolFailure("no_active_handoff", "none")

    async def attach_handoff(self, websocket: Any, handoff_id: str) -> None:
        raise AssertionError("not used")

    async def hangup(self, reason: str) -> None:
        self._ended = True

    @property
    def ended(self) -> bool:
        return self._ended

    @property
    def remote_hangup_pending(self) -> bool:
        return False

    async def wait_ended(self) -> None:
        return None


class FakeSessionFactory:
    """interfaces.CallSessionFactory：记录每次建通话的 CallSpec。"""

    def __init__(self) -> None:
        self.specs: list[m.CallSpec] = []
        self.sessions: list[FakeSession] = []

    def __call__(self, spec: m.CallSpec, deps: CallDeps) -> FakeSession:
        self.specs.append(spec)
        s = FakeSession(spec, deps)
        self.sessions.append(s)
        return s


def fake_compiled(card: m.TaskCard) -> m.CompiledCall:
    return m.CompiledCall(
        sonic_prompt="You are an AI assistant calling on behalf of the account holder (fixture).",
        jev_templates={}, policy={}, ivr_map={}, intents=[], lexicons={}, stall_phrases=["One moment, please."],
        say_templates={k: f"[{k}]" for k in m.SAY_TEMPLATE_KEYS}, allowed_fields={}, task_card=card,
        skill_versions=[m.SkillRef(name="company-lakeshore-energy", commit=COMMIT_B, content_sha256=SHA)],
    )


class RecordingCompile:
    """CallManager 的 compile_call 参数（G 线 compile_call 的替身）：在任意位置参数或关键字里找 TaskCard。"""

    def __init__(self) -> None:
        self.calls: list[tuple[tuple[Any, ...], dict[str, Any]]] = []

    def __call__(self, *args: Any, **kwargs: Any) -> m.CompiledCall:
        self.calls.append((args, kwargs))
        card = next((a for a in [*args, *kwargs.values()] if isinstance(a, m.TaskCard)), None)
        if card is None:
            raise AssertionError("compile_call 没有收到 TaskCard")
        return fake_compiled(card)


async def _noop_ended(call_id: str) -> None:
    return None


def make_call_deps(settings: Settings, store: Any, trace: Any, clock: Any, *, gate: Any = None, llm: Any = None,
                   on_ended: Callable[[str], Any] = _noop_ended, pending: Any = None) -> CallDeps:
    return CallDeps(settings=settings, store=store, trace=trace, gate=gate or ScriptedGate(),
                    pending=pending if pending is not None else FakePending(),
                    llm=llm or RecordingLlm(), clock=clock, line_tokens=FakeLineTokens(),
                    handoff_tokens=FakeHandoffTokens(), on_ended=on_ended, company_factory=None)


def make_app_services(settings: Settings, store: Any, trace: Any, clock: Any) -> AppServices:
    return AppServices(settings=settings, store=store, trace=trace, clock=clock, calls=FakeCallRegistry(),
                       line_tokens=FakeLineTokens(), handoff_tokens=FakeHandoffTokens())


# ---------------------------------------------------------------------------
# 任务卡与 9 个工具的合法输出
# ---------------------------------------------------------------------------


def option(oid: str, label: str | None = None) -> m.Option:
    return m.Option(id=oid, label=label or oid.replace("_", " ").title(), detail=f"{oid} detail")


def sample_card(version: int = 1, *, company_id: str = "lakeshore-energy", name: str = "Lakeshore Energy") -> m.TaskCard:
    return m.TaskCard(
        card_version=version,
        company=m.TaskCompany(company_id=company_id, name=name, official_number=LAKESHORE_NUMBER,
                              sources=[m.Source(url=LAKESHORE_URLS[0], title="Contact Lakeshore Energy")]),
        goal="Start electricity service at 1200 W Example St, Apt 4B on November 1",
        task_summary="Ask Lakeshore Energy to start power at 1200 W Example St, Apt 4B beginning November 1.",
        effective_date="November 1",
        shared_fields=[m.SharedField(key="name", label="Name", display="Alex Example")],
        precall_decisions=[m.PrecallDecision(question_id="deposit_or_autopay", question="Deposit or autopay?",
                                             options=[option("deposit"), option("autopay")], default="deposit",
                                             selected="deposit", needs_user=False)],
        boundary="Only start this service; no other account changes.",
    )


def canned_outputs() -> dict[str, Any]:
    """9 个工具各一个内容饱满的合法输出（嵌套对象、时间戳、概率都有值）。"""
    pending = m.PendingChoice(choice_id="choice-1", question_text="Add $25 for November 1, or November 3 at no charge?",
                              options=[option("accept"), option("decline"), option("decide_later")],
                              created_at=NOW, expires_at=NOW + timedelta(seconds=20), status="open")
    jev = {"menu_choice": m.JevAnswer(type="choice", choice="1", confidence=0.93,
                                      probabilities={"1": 0.93, "none_of_the_above": 0.07}),
           "needs_user": m.JevAnswer(type="noul", noul=0.12),
           "stakes": m.JevAnswer(type="score", score=1.0, confidence=0.9, probabilities={"0": 0.1, "1": 0.9})}
    return {
        "find_official_number": m.OfficialNumberOut(
            company_id="lakeshore-energy", company_name="Lakeshore Energy", official_number=LAKESHORE_NUMBER,
            verified=True,
            sources=[m.SourceCheck(url=LAKESHORE_URLS[0], title="Contact", number_found=LAKESHORE_NUMBER),
                     m.SourceCheck(url=LAKESHORE_URLS[1], title="Start service", number_found=LAKESHORE_NUMBER),
                     m.SourceCheck(url="https://directory.example/lakeshore", title="Directory", number_found=None)],
            reason="2 official sources agree"),
        "prepare_task": m.PrepareTaskOut(
            task_id="task-1", status="setup_required", task_card=sample_card(1),
            missing=[m.MissingField(key="account_number", label="Account number")],
            skills=[m.SkillRef(name="assisted-call", commit=COMMIT_A, content_sha256=SHA),
                    m.SkillRef(name="company-lakeshore-energy", commit="unknown", content_sha256=SHA)],
            decisions=[m.DecisionSummary(point="company_skill", choice="company-lakeshore-energy", confidence=0.97),
                       m.DecisionSummary(point="task_intent", choice="start_service", confidence=0.91)],
            message="Here is the task card."),
        "update_task": m.UpdateTaskOut(task_id="task-1", status="ready", task_card=sample_card(2), missing=[]),
        "start_assisted_call": m.StartCallOut(task_id="task-1", call_id="call-1", status="dialing"),
        "get_call_status": m.CallStatusOut(
            task_id="task-1", call_id="call-1", phase="awaiting_user", status_line="Waiting for your choice",
            transcript_tail=[m.TranscriptTailItem(speaker="company", text="Add $25 for November 1?", t_ms=61000),
                             m.TranscriptTailItem(speaker="waitless", text="One moment, please.", t_ms=62500)],
            pending_choice=pending,
            handoff=m.HandoffStatus(state="ready", company_name="Lakeshore Energy",
                                    media=m.Media(ai_media="connected", recording=False, transcription=True)),
            interrupts_used=1, elapsed_s=62.5, ended=False,
            debug=m.CallStatusDebug(
                mode="replay", skills=[m.SkillCommit(name="decide-option", commit=COMMIT_A)],
                last_decision=m.LastDecision(decision_id="dec-9", point="call_round", intent="ask_user",
                                             action="ask_user", reason_code="consequential", jev=jev, latency_ms=320),
                guard=m.GuardCounters(blocked=2, released=3))),
        "answer_pending_choice": m.AnswerChoiceOut(task_id="task-1", choice_id="choice-1", status="choice_recorded",
                                                   option_id="decline"),
        "join_call": m.JoinCallOut(task_id="task-1", call_id="call-1", handoff_id="h-1", ws_path="/ws/handoff/h-1",
                                   token="handoff-token-1", expires_at=NOW + timedelta(minutes=2),
                                   company_name="Lakeshore Energy",
                                   media=m.JoinMedia(ai_media="disconnected", recording=False, transcription=False)),
        "return_to_agent": m.ReturnOut(task_id="task-1", call_id="call-1", status="resuming",
                                       media=m.ReturnMedia(ai_media="connected", recording=False, transcription=True)),
        "get_case_result": m.CaseResultOut(
            task_id="task-1", company_name="Lakeshore Energy", outcome="confirmed", headline="Service starts November 1",
            verified=[m.VerifiedItem(label="Start date", value="November 1", evidence=m.Evidence(call_id="call-1", turn_seq=41))],
            follow_ups=[m.FollowUpItem(label="Deposit refund", detail="Refund after 12 on-time payments")],
            confirmation_number="LE-48291", summary="Service is confirmed for November 1.",
            calls=[m.CallSummary(call_id="call-1", scenario_id="baseline", started_at=NOW, ended_at=NOW + timedelta(minutes=3),
                                 duration_s=180.0, outcome="confirmed", interrupts_used=1)],
            history=[m.HistoryItem(t=NOW, title="Call started", detail="call-1"),
                     m.HistoryItem(t=NOW + timedelta(minutes=3), title="Call ended", detail="confirmed")]),
    }


# 9 个工具的一组合法入参（tech §6.1）
TOOL_ARGS: dict[str, dict[str, Any]] = {
    "find_official_number": {"company": "Lakeshore Energy", "intent": "start electric service"},
    "prepare_task": {"request": "Start electricity at my new apartment on November 1", "provider_hint": "Lakeshore Energy"},
    "update_task": {"task_id": "task-1", "fields": {"name": "Alex Example"}, "decisions": {"deposit_or_autopay": "autopay"}},
    "start_assisted_call": {"task_id": "task-1", "confirmed": True, "card_version": 2},
    "get_call_status": {"task_id": "task-1", "debug": True},
    "answer_pending_choice": {"task_id": "task-1", "choice_id": "choice-1", "option_id": "decline"},
    "join_call": {"task_id": "task-1"},
    "return_to_agent": {"task_id": "task-1", "handoff_id": "h-1"},
    "get_case_result": {"task_id": "task-1"},
}


class StubHandlers(ToolHandlers):
    """ToolHandlers 的替身：不调用父类构造；9 个方法记录入参并返回 outputs[工具名]，或抛 fail[工具名]。"""

    def __init__(self, outputs: Mapping[str, Any] | None = None, *, fail: Mapping[str, BaseException] | None = None) -> None:  # noqa: D107
        self.outputs = dict(outputs or canned_outputs())
        self.fail = dict(fail or {})
        self.calls: list[tuple[str, dict[str, Any]]] = []

    def calls_for(self, tool: str) -> list[dict[str, Any]]:
        return [kw for t, kw in self.calls if t == tool]

    async def _respond(self, tool: str, **kwargs: Any) -> Any:
        self.calls.append((tool, kwargs))
        if tool in self.fail:
            raise self.fail[tool]
        return self.outputs[tool]

    async def find_official_number(self, company: str, intent: str) -> m.OfficialNumberOut:
        return await self._respond("find_official_number", company=company, intent=intent)

    async def prepare_task(self, request: str, provider_hint: str | None = None) -> m.PrepareTaskOut:
        return await self._respond("prepare_task", request=request, provider_hint=provider_hint)

    async def update_task(self, task_id: str, fields: m.UpdateTaskFields | None = None,
                          decisions: dict[str, str] | None = None) -> m.UpdateTaskOut:
        return await self._respond("update_task", task_id=task_id, fields=fields, decisions=decisions)

    async def start_assisted_call(self, task_id: str, confirmed: bool, card_version: int) -> m.StartCallOut:
        return await self._respond("start_assisted_call", task_id=task_id, confirmed=confirmed, card_version=card_version)

    async def get_call_status(self, task_id: str, debug: bool = False) -> m.CallStatusOut:
        return await self._respond("get_call_status", task_id=task_id, debug=debug)

    async def answer_pending_choice(self, task_id: str, choice_id: str, option_id: str) -> m.AnswerChoiceOut:
        return await self._respond("answer_pending_choice", task_id=task_id, choice_id=choice_id, option_id=option_id)

    async def join_call(self, task_id: str) -> m.JoinCallOut:
        return await self._respond("join_call", task_id=task_id)

    async def return_to_agent(self, task_id: str, handoff_id: str) -> m.ReturnOut:
        return await self._respond("return_to_agent", task_id=task_id, handoff_id=handoff_id)

    async def get_case_result(self, task_id: str) -> m.CaseResultOut:
        return await self._respond("get_case_result", task_id=task_id)


def stub_services(settings: Settings) -> AppServices:
    """build_mcp 只需要 settings 的场景：其余服务给占位对象（StubHandlers 不会用到它们）。"""
    return AppServices(settings=settings, store=None, trace=None, clock=None, calls=FakeCallRegistry(),  # type: ignore[arg-type]
                       line_tokens=FakeLineTokens(), handoff_tokens=FakeHandoffTokens())


# ---------------------------------------------------------------------------
# 官方 MCP Python SDK 客户端（mcp==2.3.0）
# ---------------------------------------------------------------------------


@asynccontextmanager
async def mcp_session(app: Any, *, base_url: str = BASE_URL, mode: str = "legacy"):
    """跑 app 的 lifespan（MCP 的 session_manager 在父 lifespan 里，inv-1 §2.1），再用官方 SDK 客户端
    经 Streamable HTTP 连 {base_url}/mcp。mode="legacy" 走 2025-11-25 的 initialize 握手。"""
    import httpx2
    from mcp import Client
    from mcp.client.streamable_http import streamable_http_client

    async with AsyncExitStack() as stack:
        await stack.enter_async_context(app.router.lifespan_context(app))
        http = await stack.enter_async_context(
            httpx2.AsyncClient(transport=httpx2.ASGITransport(app=app), base_url=base_url))
        client = await stack.enter_async_context(
            Client(streamable_http_client(f"{base_url}/mcp", http_client=http), mode=mode))
        yield client


def mcp_app_for(server: Any) -> Any:
    """测试自己把 build_mcp() 的 MCPServer 包成独立 ASGI 应用（只用于 build_mcp 层的测试；
    挂载与 DNS-rebinding 白名单是 create_app 的事，另有测试）。"""
    from mcp.server.transport_security import TransportSecuritySettings

    return server.streamable_http_app(
        stateless_http=True, json_response=True, streamable_http_path="/mcp",
        transport_security=TransportSecuritySettings(enable_dns_rebinding_protection=False),
    )


def error_payload(result: Any) -> dict[str, Any]:
    """isError 结果的 content[0].text（JSON）。"""
    assert result.is_error is True, f"expected isError, got {result!r}"
    assert result.content, "isError result has no content"
    first = result.content[0]
    assert getattr(first, "type", None) == "text"
    return json.loads(first.text)


def jsonrpc(method: str, params: Mapping[str, Any] | None = None, *, rid: int | None = 1) -> dict[str, Any]:
    msg: dict[str, Any] = {"jsonrpc": "2.0", "method": method}
    if params is not None:
        msg["params"] = dict(params)
    if rid is not None:
        msg["id"] = rid
    return msg


INIT_PARAMS = {"protocolVersion": "2025-11-25", "capabilities": {},
               "clientInfo": {"name": "waitless-test-client", "version": "0.0.0"}}
MCP_HEADERS = {"Accept": "application/json, text/event-stream", "Content-Type": "application/json"}


# ---------------------------------------------------------------------------
# trace 事件构造
# ---------------------------------------------------------------------------


def decision_event(task_id: str, call_id: str | None, decision_id: str, *, point: str = "call_round",
                   t_ms: int | None = None, turn_seq: int | None = None) -> m.TraceEvent:
    """定稿 A-08：call_round 的 decision 事件在 action_json 写 {turn_seq: CompanyTurn.seq}。"""
    return m.TraceEvent(
        task_id=task_id, call_id=call_id, t_ms=t_ms, event_type="decision", decision_id=decision_id,
        decision_point=point, action={"turn_seq": turn_seq} if turn_seq is not None else None,
        jev_request={"model": "fake-jev", "state": {"task_goal": "g"}, "questions": {"menu_choice": {"type": "choice"}}},
        jev_response={"answers": {"menu_choice": {"type": "choice", "choice": "1", "confidence": 0.93}}},
        policy_decision={"intent": "press", "policy_rule": "auto_high_conf"}, latency_ms=300,
    )


def company_turn(task_id: str, call_id: str, text: str, *, kind: str = "speech", turn_seq: int = 0,
                 t_ms: int | None = None) -> m.TraceEvent:
    """company_turn：action={"kind", "seq": CompanyTurn.seq}；定稿 A-08：t_ms 取 CompanyTurn.t_end_ms。"""
    return m.TraceEvent(task_id=task_id, call_id=call_id, t_ms=t_ms, event_type="company_turn", speaker="company",
                        transcript=text, action={"kind": kind, "seq": turn_seq})


def utterance(task_id: str, call_id: str, text: str, *, t_ms: int | None = None) -> m.TraceEvent:
    return m.TraceEvent(task_id=task_id, call_id=call_id, t_ms=t_ms, event_type="waitless_utterance",
                        speaker="waitless", transcript=text)


def consequential(event_type: str, task_id: str, call_id: str, decision_id: str, *, t_ms: int | None = None,
                  action: Mapping[str, Any] | None = None) -> m.TraceEvent:
    default_action = {"dtmf_sent": {"digits": "1"}, "injection_sent": {"text": "Please hold.", "template": None}}
    return m.TraceEvent(task_id=task_id, call_id=call_id, t_ms=t_ms, event_type=event_type, decision_id=decision_id,
                        action=dict(action) if action is not None else default_action.get(event_type))


def user_action(task_id: str, call_id: str | None, name: str, shown_ms: int, acted_ms: int, *,
                t_ms: int | None = None) -> m.TraceEvent:
    return m.TraceEvent(task_id=task_id, call_id=call_id, t_ms=t_ms, event_type="user_action", speaker="user",
                        action={"name": name, "shown_ms": shown_ms, "acted_ms": acted_ms})


def plain(event_type: str, task_id: str, call_id: str | None, *, t_ms: int | None = None,
          decision_id: str | None = None) -> m.TraceEvent:
    return m.TraceEvent(task_id=task_id, call_id=call_id, t_ms=t_ms, event_type=event_type, decision_id=decision_id)


# ---------------------------------------------------------------------------
# 组装：应用与 A 线服务（共用一个 Store、Trace、FakeClock）
# ---------------------------------------------------------------------------


def build_app(root: Path, *, mode: str = "replay", allowed_hosts: Sequence[str] = (LOOPBACK, "localhost"),
              web: bool = True, gate: Any = None, llm: Any = None) -> tuple[Any, Settings, Any]:
    """create_app(settings, services=..., parts=AppParts(...))（缺口 A-G-1，r3 定稿）：AppParts 用 A 线真服务
    （TaskService、CallManager、ResultService、AssistantService）加别的线的替身组装，不依赖 G/D/E 的实现。
    返回 (app, settings, store)；调用方负责 store.close()。需要 AEnv 本身时用 AEnv(root).app(...)。"""
    env = AEnv(root, mode=mode, allowed_hosts=allowed_hosts)
    if web:
        write_web(env.settings.web_dir)
    return env.app(gate=gate, llm=llm), env.settings, env.store


LAKESHORE_REQUEST = ("Please start electricity at my new apartment, 1200 W Example St, Apt 4B, "
                     "with Lakeshore Energy on November 1.")
LAKESHORE_EXTRACTED = m.ExtractedRequest(company_name="Lakeshore Energy", intent_text="start electricity service",
                                         effective_date="November 1", service_address="1200 W Example St, Apt 4B",
                                         stated_preferences=[])


class AEnv:
    """A 线服务的测试组装：真 Store、真 Trace（A 线代码）、FakeClock；别的线一律用替身。

    所有 A 线服务共用同一个 FakeClock（r3：ResultService 也收 clock，缺口 A-G-3），时间只随测试推进。
    """

    def __init__(self, root: Path, *, mode: str = "text-sim", skills: Mapping[str, m.Skill] | None = None,
                 allowed_hosts: Sequence[str] = (LOOPBACK, "localhost")) -> None:
        from waitless.store import Store
        from waitless.trace import Trace

        self.root = root
        self.clock = FakeClock()
        self.settings = make_settings(root / "env", mode=mode, allowed_hosts=allowed_hosts)
        self.store = Store(self.settings.db_path)
        self.store.init()
        self.trace = Trace(self.store, self.clock)
        self.skills = dict(skills) if skills is not None else build_skills(root / "skills-fixture")
        self.factory = FakeSessionFactory()
        self.compile = RecordingCompile()

    def close(self) -> None:
        self.store.close()

    # -- 服务 ----------------------------------------------------------------
    def task_service(self, *, gate: ScriptedGate, llm: Any = None, fetcher: Any = None) -> Any:
        from waitless.tasks import TaskService

        if getattr(gate, "trace", None) is None:
            gate.trace = self.trace
        return TaskService(store=self.store, trace=self.trace, gate=gate,
                           llm=llm if llm is not None else RecordingLlm(extracted=LAKESHORE_EXTRACTED),
                           skills=self.skills, fetcher=fetcher if fetcher is not None else DictFetcher(lakeshore_pages()),
                           clock=self.clock)

    def call_manager(self, *, gate: Any = None, llm: Any = None, results: Any = None, pending: Any = None) -> Any:
        """CallManager(..., results=ResultService)（缺口 A-G-8，r3 定稿）；on_ended 经 results.finalize 走结果流程。"""
        from waitless.calls import CallManager

        if results is None:
            results = self.result_service(gate=gate if gate is not None else ScriptedGate(),
                                          llm=llm if llm is not None else RecordingLlm())
        deps = make_call_deps(self.settings, self.store, self.trace, self.clock, gate=gate, llm=llm, pending=pending)
        manager = CallManager(settings=self.settings, store=self.store, trace=self.trace, compile_call=self.compile,
                              session_factory=self.factory, deps=deps, results=results)
        deps.on_ended = manager.on_ended
        return manager

    def result_service(self, *, gate: ScriptedGate, llm: Any) -> Any:
        """ResultService(store, trace, gate, llm, skills, clock)（缺口 A-G-3，r3 定稿）。finalize 返回 CallResult（A-24）。"""
        from waitless.results import ResultService

        if getattr(gate, "trace", None) is None:
            gate.trace = self.trace
        return ResultService(store=self.store, trace=self.trace, gate=gate, llm=llm, skills=self.skills, clock=self.clock)

    def assistant(self, *, gate: Any, llm: Any = None) -> Any:
        from waitless.assistant import AssistantService

        if getattr(gate, "trace", None) is None:
            gate.trace = self.trace
        return AssistantService(gate=gate, llm=llm if llm is not None else RecordingLlm(extracted=LAKESHORE_EXTRACTED),
                                skills=self.skills, trace=self.trace, clock=self.clock)

    def parts(self, *, gate: Any = None, llm: Any = None, fetcher: Any = None) -> Any:
        """app.AppParts：A 线真服务共用一个决策门替身与一个 L1–L5 替身（缺口 A-G-1）。"""
        from waitless.app import AppParts

        gate = gate if gate is not None else default_prepare_gate()
        if getattr(gate, "trace", None) is None:
            gate.trace = self.trace
        llm = llm if llm is not None else RecordingLlm(extracted=LAKESHORE_EXTRACTED)
        fetcher = fetcher if fetcher is not None else DictFetcher(lakeshore_pages())
        results = self.result_service(gate=gate, llm=llm)
        self.calls = self.call_manager(gate=gate, llm=llm, results=results)
        trusted = m.TrustedSources.model_validate(read_yaml(self.skills["find-official-number"],
                                                            "references/trusted-sources.yaml"))
        return AppParts(tasks=self.task_service(gate=gate, llm=llm, fetcher=fetcher), calls=self.calls, results=results,
                        assistant=self.assistant(gate=gate, llm=llm), fetcher=fetcher, trusted_sources=trusted)

    def app(self, *, gate: Any = None, llm: Any = None, fetcher: Any = None) -> Any:
        from waitless.app import create_app

        parts = self.parts(gate=gate, llm=llm, fetcher=fetcher)
        services = AppServices(settings=self.settings, store=self.store, trace=self.trace, clock=self.clock,
                               calls=parts.calls, line_tokens=FakeLineTokens(), handoff_tokens=FakeHandoffTokens())
        return create_app(self.settings, services=services, parts=parts)

    # -- 数据 ----------------------------------------------------------------
    async def ready_task(self, *, gate: Any = None) -> tuple[str, int]:
        """经 TaskService.prepare 得到一个 ready 的任务（档案齐全），返回 (task_id, card_version)。
        要开始通话的测试用它：ready 时刻由 TaskService 记下，CallManager.start 写 user_action 时要用（A-G-9）。"""
        self.store.save_profile({k: PROFILE_VALUES[k] for k in ("name", "phone_number", "address")})
        self.store.save_profile({"account_number": PROFILE_VALUES["account_number"]}, company_id="lakeshore-energy")
        out = await self.task_service(gate=gate if gate is not None else default_prepare_gate()).prepare(
            LAKESHORE_REQUEST, None)
        assert out.status == "ready" and out.task_card is not None, out
        return out.task_id, out.task_card.card_version

    def add_task(self, task_id: str, *, version: int = 2, status: str = "ready",
                 company_id: str = "lakeshore-energy", company_name: str = "Lakeshore Energy") -> str:
        card = sample_card(version, company_id=company_id, name=company_name)
        self.store.create_task(task_id=task_id, company_id=company_id, goal=card.goal, request_text=LAKESHORE_REQUEST,
                               task_card=card.model_dump(mode="json"), card_version=version, status=status)
        return task_id

    def add_call(self, task_id: str, call_id: str, lines: Sequence[tuple[str, str]], *, step_ms: int = 4000,
                 scenario_id: str = "baseline") -> list[int]:
        """写一通刚结束的电话：calls 行 + call_started、逐句转写（每句前推进 step_ms）、call_ended 事件。
        返回每句转写事件的 seq。ended_at 留给 finalize。"""
        started_iso = self.clock.now_iso()
        self.store.create_call(call_id=call_id, task_id=task_id, scenario_id=scenario_id, mode=self.settings.mode,
                               started_at=started_iso,
                               skill_commits=[{"name": "company-lakeshore-energy", "commit": COMMIT_B}])
        self.trace.event(plain("call_started", task_id, call_id, t_ms=self.clock.monotonic_ms()))
        seqs: list[int] = []
        for i, (speaker, text) in enumerate(lines):
            self.clock.advance_sync(step_ms)
            t = self.clock.monotonic_ms()
            ev = (company_turn(task_id, call_id, text, turn_seq=i, t_ms=t) if speaker == "company"
                  else utterance(task_id, call_id, text, t_ms=t))
            seqs.append(self.trace.event(ev))
        self.clock.advance_sync(step_ms)
        self.trace.event(plain("call_ended", task_id, call_id, t_ms=self.clock.monotonic_ms()))
        return seqs  # ended_at、duration_s 由 ResultService.finalize 按 clock 写（缺口 A-G-3）


def db_bytes(db_path: Path) -> bytes:
    """数据库文件及其 -wal、-journal、-shm 的全部字节（查诱饵值是否落盘）。"""
    out = b""
    for p in sorted(db_path.parent.glob(db_path.name + "*")):
        if p.is_file():
            out += p.read_bytes()
    return out


__all__ = [name for name in dir() if not name.startswith("_")]
