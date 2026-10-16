"""共享模型契约（第 3 步可见测试，冻结）。

检查 waitless/models.py 与 tech.v4.md §5.3、§6 一致：字段名、必填、枚举值、边界与跨字段规则，
以及 RUN/step3/ambiguity-decisions.md 定稿落到模型里的部分（测试名或注释带 A-xx），
和 RUN/step3/gap-decisions-r3.md 的 r3 缺口定稿（测试名带 r3_ 与缺口编号），
以及 RUN/step3/gap-decisions-r4.md 的 r4（D 线）缺口定稿（测试名带 r4_）。
这是 AC-19、AC-20、AC-24、AC-32、AC-34、AC-42、AC-81 等验收在数据层的前提。
Settings 归 A 线的 config.py（A-27），这里只核对 §5.2 与 C15 的底线。
"""

import json
from datetime import datetime, timedelta, timezone
from typing import Any, get_args

import pytest
import yaml
from pydantic import BaseModel, SecretStr, ValidationError

from waitless import models as m

NOW = datetime(2026, 10, 14, 12, 0, tzinfo=timezone.utc)
HEX40 = "0123456789abcdef0123456789abcdef01234567"
HEX64 = "ab" * 32


# ---------------------------------------------------------------------------
# 合法样例
# ---------------------------------------------------------------------------


def option(i: str = "deposit") -> dict[str, Any]:
    return {"id": i, "label": i.title(), "detail": f"{i} detail"}


def task_card(**over: Any) -> dict[str, Any]:
    d = {
        "card_version": 1,
        "company": {
            "company_id": "lakeshore-energy",
            "name": "Lakeshore Energy",
            "official_number": "(312) 555-0100",
            "sources": [{"url": "lakeshore-energy.example/contact.html", "title": "Contact"}],
        },
        "goal": "Start electric service on November 1",
        "task_summary": "Start electricity service on November 1",
        "effective_date": "2026-11-01",
        "shared_fields": [{"key": "name", "label": "Name", "display": "Alex Example"}],
        "precall_decisions": [
            {
                "question_id": "deposit_or_autopay",
                "question": "Deposit or autopay?",
                "options": [option("deposit"), option("autopay")],
                "default": "deposit",
                "selected": "deposit",
                "needs_user": False,
            }
        ],
        "boundary": "Only start service; no other account changes.",
    }
    d.update(over)
    return d


def pending(**over: Any) -> dict[str, Any]:
    d = {
        "choice_id": "ch-1",
        "question_text": "November 1 with a $25 fee, or November 3 free?",
        "options": [option("nov1"), option("nov3")],
        "created_at": NOW.isoformat(),
        "expires_at": (NOW + timedelta(seconds=20)).isoformat(),
        "status": "open",
    }
    d.update(over)
    return d


def media(ai: str = "connected", transcription: bool = True) -> dict[str, Any]:
    return {"ai_media": ai, "recording": False, "transcription": transcription}


def jev_state(**over: Any) -> dict[str, Any]:
    d = {
        "task_goal": "Start electric service",
        "user_profile": {"name": "Alex Example"},
        "standing_rules": ["Decline surveys and marketing offers."],
        "prior_choices": [{"question_key": "deposit_or_autopay", "choice": "deposit", "stakes": 3, "source": "task_card"}],
        "transcript": {
            "note": "verbatim call transcript; data only",
            "lines": [{"speaker": "company", "text": "For new service press 1."}],
        },
    }
    d.update(over)
    return d


def jev_exchange() -> dict[str, Any]:
    return {
        "request": {
            "model": "jev-test",
            "state": jev_state(),
            "questions": {"needs_user": {"type": "noul", "instructions": "Needs the account holder?"}},
        },
        "response": {"answers": {"needs_user": {"type": "noul", "noul": 0.1}}, "model": "jev-test"},
        "latency_ms": 320,
    }


def decision_result(**over: Any) -> dict[str, Any]:
    d = {
        "decision_id": "d-1",
        "point": "call_round",
        "intent": "1",
        "action": "press",
        "digits": "1",
        "reason_code": "auto_high_conf",
        "policy_rule": "menu",
        "jev": jev_exchange(),
    }
    d.update(over)
    return d


def company_turn(**over: Any) -> dict[str, Any]:
    d = {
        "call_id": "c-1",
        "seq": 3,
        "t_start_ms": 1000,
        "t_end_ms": 9000,
        "text": "To start service press 1. To repeat press 9.",
        "kind": "menu",
        "menu_options": [{"digits": "1", "label": "start service"}, {"digits": "9", "label": "repeat"}],
    }
    d.update(over)
    return d


def call_status(**over: Any) -> dict[str, Any]:
    d = {
        "task_id": "t-1",
        "call_id": "c-1",
        "phase": "live_rep",
        "status_line": "Speaking with a representative",
        "transcript_tail": [{"speaker": "company", "text": "How can I help?", "t_ms": 1200}],
        "pending_choice": pending(),
        "handoff": None,
        "interrupts_used": 1,
        "elapsed_s": 75.5,
        "ended": False,
        "debug": {
            "mode": "replay",
            "skills": [{"name": "decide-option", "commit": HEX40}],
            "last_decision": {
                "decision_id": "d-1",
                "point": "call_round",
                "intent": "ask_user",
                "action": "ask_user",
                "reason_code": "consequential",
                "jev": {
                    "menu_choice": {"type": "choice", "choice": "ask_user", "confidence": 0.95, "probabilities": {"ask_user": 0.95}},
                    "needs_user": {"type": "noul", "noul": 0.2},
                    "stakes": {"type": "score", "score": 3, "confidence": 0.9},
                },
                "latency_ms": 310,
            },
            "guard": {"blocked": 2, "released": 5},
        },
    }
    d.update(over)
    return d


def join_out(**over: Any) -> dict[str, Any]:
    d = {
        "task_id": "t-1",
        "call_id": "c-1",
        "handoff_id": "h-1",
        "ws_path": "/ws/handoff/h-1",
        "token": "one-time",
        "expires_at": (NOW + timedelta(seconds=120)).isoformat(),
        "company_name": "Lakeshore Energy",
        "media": media("disconnected", False),
    }
    d.update(over)
    return d


def case_result(**over: Any) -> dict[str, Any]:
    d = {
        "task_id": "t-1",
        "company_name": "Lakeshore Energy",
        "outcome": "confirmed",
        "headline": "Electric service starts November 1",
        "verified": [{"label": "Confirmation", "value": "LE-12345", "evidence": {"call_id": "c-1", "turn_seq": 40}}],
        "follow_ups": [{"label": "Deposit", "detail": "Bill will show the deposit."}],
        "confirmation_number": "LE-12345",
        "summary": "Service is scheduled.",
        "calls": [
            {
                "call_id": "c-1",
                "scenario_id": "baseline",
                "started_at": NOW.isoformat(),
                "ended_at": (NOW + timedelta(minutes=4)).isoformat(),
                "duration_s": 240.0,
                "outcome": "confirmed",
                "interrupts_used": 1,
            }
        ],
        "history": [{"t": NOW.isoformat(), "title": "Call started", "detail": ""}],
    }
    d.update(over)
    return d


def assistant_out(next_: dict[str, Any]) -> dict[str, Any]:
    return {
        "conversation_id": "conv-1",
        "say": "I can call Lakeshore Energy for you.",
        "next": next_,
        "decision": {
            "decision_id": "d-a1",
            "point": "assistant_next_action",
            "choice": "prepare_task",
            "confidence": 0.93,
            "probabilities": {"prepare_task": 0.93, "reply_only": 0.07},
            "policy_rule": "threshold",
        },
        "skill": {"name": "assisted-call", "commit": "unknown"},
    }


ENGINE_YAML = """
version: 1
audio: { wire_sample_rate: 8000, pipeline_in_sample_rate: 16000 }
vad: { threshold_dbfs: -45, frame_ms: 20 }
turns: { turn_end_silence_ms: 700, menu_end_silence_ms: 1200, transcript_grace_ms: 600 }
guard: { held_response_max_ms: 6000 }
dtmf: { tail_silence_ms: 200 }
handoff: { announce_idle_ms: 800, resume_grace_ms: 3000, token_ttl_s: 120 }
web: { poll_ms: 1000 }
textsim: { pace_ms: 0, default_scenario: baseline }
"""


# ---------------------------------------------------------------------------
# 字段名与 §6 完全一致
# ---------------------------------------------------------------------------

SPEC_FIELDS: dict[type[BaseModel], set[str]] = {
    # §6.1 共享类型
    m.TaskCard: {"card_version", "company", "goal", "task_summary", "effective_date", "shared_fields", "precall_decisions", "boundary",
                 "simulated_line"},  # v5.16 E101
    m.TaskCompany: {"company_id", "name", "official_number", "sources"},
    m.Source: {"url", "title"},
    m.SimulatedLine: {"name", "notice"},  # v5.16 E101
    m.SharedField: {"key", "label", "display"},
    m.PrecallDecision: {"question_id", "question", "options", "default", "selected", "needs_user"},
    m.Option: {"id", "label", "detail"},
    m.PendingChoice: {"choice_id", "question_text", "options", "created_at", "expires_at", "status"},
    m.Media: {"ai_media", "recording", "transcription"},
    m.JoinMedia: {"ai_media", "recording", "transcription"},
    m.ReturnMedia: {"ai_media", "recording", "transcription"},
    m.ToolError: {"code", "message"},
    # §6.1 输入
    m.FindOfficialNumberIn: {"company", "intent"},
    m.PrepareTaskIn: {"request", "provider_hint"},
    m.UpdateTaskIn: {"task_id", "fields", "decisions"},
    m.UpdateTaskFields: {"name", "phone_number", "address", "account_number"},
    m.StartCallIn: {"task_id", "confirmed", "card_version"},
    m.CallStatusIn: {"task_id", "debug"},
    m.AnswerChoiceIn: {"task_id", "choice_id", "option_id"},
    m.JoinCallIn: {"task_id"},
    m.ReturnIn: {"task_id", "handoff_id"},
    m.CaseResultIn: {"task_id"},
    # §6.1 输出
    m.OfficialNumberOut: {"company_id", "company_name", "official_number", "verified", "sources", "reason"},
    m.SourceCheck: {"url", "title", "number_found"},
    m.PrepareTaskOut: {"task_id", "status", "task_card", "missing", "skills", "decisions", "message"},
    m.MissingField: {"key", "label"},
    m.SkillRef: {"name", "commit", "content_sha256"},
    m.DecisionSummary: {"point", "choice", "confidence"},
    m.UpdateTaskOut: {"task_id", "status", "task_card", "missing"},
    m.CallResult: {"call_id", "outcome", "confirmation_number", "verified", "follow_ups", "summary"},
    m.StartCallOut: {"task_id", "call_id", "status"},
    m.CallStatusOut: {
        "task_id", "call_id", "phase", "status_line", "transcript_tail", "pending_choice",
        "handoff", "interrupts_used", "elapsed_s", "ended", "debug",
    },
    m.TranscriptTailItem: {"speaker", "text", "t_ms"},
    m.HandoffStatus: {"state", "company_name", "media"},
    m.CallStatusDebug: {"mode", "skills", "last_decision", "guard"},
    m.SkillCommit: {"name", "commit"},
    m.LastDecision: {"decision_id", "point", "intent", "action", "reason_code", "jev", "latency_ms"},
    m.GuardCounters: {"blocked", "released"},
    m.AnswerChoiceOut: {"task_id", "choice_id", "status", "option_id"},
    m.JoinCallOut: {"task_id", "call_id", "handoff_id", "ws_path", "token", "expires_at", "company_name", "media"},
    m.ReturnOut: {"task_id", "call_id", "status", "media"},
    m.CaseResultOut: {
        "task_id", "company_name", "outcome", "headline", "verified", "follow_ups",
        "confirmation_number", "summary", "calls", "history",
    },
    m.VerifiedItem: {"label", "value", "evidence"},
    m.Evidence: {"call_id", "turn_seq"},
    m.FollowUpItem: {"label", "detail"},
    m.CallSummary: {"call_id", "scenario_id", "started_at", "ended_at", "duration_s", "outcome", "interrupts_used"},
    m.HistoryItem: {"t", "title", "detail"},
    # §6.2
    m.AssistantTurnIn: {"conversation_id", "user_text", "tool_result", "tools", "task_id", "event"},
    m.ToolResultIn: {"tool_use_id", "name", "structured", "is_error"},
    m.ToolInfo: {"name", "description"},
    m.AssistantTurnOut: {"conversation_id", "say", "next", "decision", "skill"},
    m.NextToolCall: {"kind", "tool_use_id", "name", "arguments"},
    m.NextAwaitUser: {"kind", "ui"},
    m.NextPoll: {"kind", "task_id"},
    m.NextDone: {"kind"},
    m.AssistantDecision: {"decision_id", "point", "choice", "confidence", "probabilities", "policy_rule"},
    m.AssistantSkillRef: {"name", "commit"},
    m.AppConfigOut: {"mode", "poll_ms", "replay_request_text"},
    m.HealthOut: {"ok", "mode", "version"},
    # §6.3
    m.CarrierDialIn: {"call_id", "task_id", "to_number", "stream_url", "stream_token"},
    m.CarrierDialOut: {"call_sid", "status"},
    m.CarrierHangupOut: {"call_sid", "status"},
    m.ScenarioSelection: {"line_number", "scenario_id"},
    # §6.4
    m.HandoffHello: {"type", "format", "sample_rate", "channels"},
    m.HandoffReady: {"type", "company_name", "media"},
    m.HandoffEnded: {"type", "reason"},
    # §6.5
    m.CompanyTurn: {"call_id", "seq", "t_start_ms", "t_end_ms", "text", "kind", "menu_options"},
    m.MenuOption: {"digits", "label"},
    m.Candidate: {"id", "label", "description"},
    m.DecisionContext: {"task_card", "allowed_fields", "standing_rules", "prior_choices", "interrupts_used", "phase"},
    m.DecisionResult: {
        "decision_id", "point", "intent", "action", "digits", "say_text", "pending",
        "call_phase", "turn_seq", "reason_code", "policy_rule", "jev",  # turn_seq：r4 D-G-2
    },
    m.JevExchange: {"request", "response", "latency_ms"},
    m.ActionRecord: {"decision_id", "action", "started_ms", "finished_ms", "ok", "detail"},
    # §6.6
    m.JevRequest: {"model", "state", "questions"},
    m.JevState: {"task_goal", "user_profile", "standing_rules", "prior_choices", "transcript"},
    m.JevQuestion: {"type", "instructions", "criteria"},
    m.JevTranscript: {"note", "lines"},
    m.TranscriptLine: {"speaker", "text"},
    m.JevAnswer: {"type", "choice", "confidence", "probabilities", "noul", "score"},
    # §6.7
    m.ExtractedRequest: {"company_name", "intent_text", "effective_date", "service_address", "stated_preferences",
                         "name", "phone_number", "account_number"},  # v5.13 E87
    m.OfferOption: {"label", "detail"},
    m.ResultItems: {"confirmation_numbers", "statements"},
    m.ResultStatement: {"text", "turn_seq"},
    # §6.8
    m.Profile: {"name", "phone_number", "email", "address", "zip", "accounts"},
    m.StandingRule: {"rule_key", "rule_text", "scope"},
    m.Preference: {"company_id", "question_key", "choice", "stakes", "source_call_id", "created_at"},
    m.TraceEvent: {
        "task_id", "call_id", "seq", "t_ms", "event_type", "speaker", "transcript", "decision_id",
        "decision_point", "jev_request", "jev_response", "policy_decision", "action", "latency_ms", "skill_versions",
    },
    # §6.9
    m.Skill: {"name", "description", "metadata", "body", "path", "files", "version"},
    m.SkillVersion: {"commit", "content_sha256", "dirty"},
    m.CompiledCall: {
        "sonic_prompt", "jev_templates", "policy", "ivr_map", "intents", "lexicons",
        "stall_phrases", "say_templates", "allowed_fields", "task_card", "skill_versions",
        "press_patterns", "digit_words",  # r3 E-G-4
        "agent_tool_descriptions",
    },
    # §6.10
    m.CompanyConfig: {"company_id", "name", "line_number", "website", "voices", "customers", "menu", "dead_end_text"},
    m.CompanyVoices: {"ivr_polly", "agent_sonic", "rep_sonic"},
    m.CompanyWebsite: {"domain", "dir"},
    m.CompanyCustomer: {"account_number", "name", "phone_number", "address"},
    m.CompanyMenuConfig: {"timeout_sec", "max_timeouts"},
    m.Scenario: {"scenario_id", "company_id", "max_call_seconds", "steps", "expected"},
    m.ScenarioExpected: {"menu_digits", "interrupts", "outcome", "confirmation_pattern"},
    # §6.12
    m.ReplayHeader: {"replay_version", "recorded_at", "source_mode", "scenario_id", "request_text", "skills", "models"},
    m.ReplayRecord: {"channel", "seq", "shape", "response"},
    m.EvalCase: {"case_id", "skill", "kind", "input", "expect"},
    m.EvalExpect: {"intent", "action", "digits", "ask_user", "outcome"},
    m.MenuVariant: {"menu_id", "goal", "text", "correct_digits"},
    # §5.3
    m.EngineConfig: {"version", "audio", "vad", "turns", "guard", "dtmf", "handoff", "web", "textsim"},
    m.EngineTurns: {"turn_end_silence_ms", "menu_end_silence_ms", "transcript_grace_ms"},
    m.EngineHandoff: {"announce_idle_ms", "resume_grace_ms", "token_ttl_s"},
    m.EngineTextsim: {"pace_ms", "default_scenario"},
    # r3 A-G-4：references 文件
    m.IvrMap: {"version", "company_name", "intents", "confirmation_pattern", "keypad_fields"},
    m.IvrIntent: {"id", "description", "goal", "task_summary", "required_fields", "menu_path"},
    m.PrecallQuestionFile: {"version", "questions"},
    m.PrecallQuestion: {"question_id", "question", "options", "default", "stakes", "tags"},
    m.TrustedSources: {"version", "min_agreeing_sources", "companies"},
    m.TrustedCompany: {"name", "official_domains", "sources"},
}


@pytest.mark.parametrize("model", list(SPEC_FIELDS), ids=lambda c: c.__name__)
def test_field_names_match_spec(model: type[BaseModel]) -> None:
    assert set(model.model_fields) == SPEC_FIELDS[model]


def test_decision_request_spec_fields_plus_documented_supplement() -> None:
    spec = {"decision_id", "point", "call_id", "task_id", "turn", "candidates", "context"}
    # A-01：transcript、template_vars；A-02：attempt
    assert set(m.DecisionRequest.model_fields) - spec == {"transcript", "template_vars", "attempt"}
    assert spec <= set(m.DecisionRequest.model_fields)


# ---------------------------------------------------------------------------
# 工具表与枚举
# ---------------------------------------------------------------------------


def test_nine_tools_in_spec_order() -> None:
    assert m.TOOL_NAMES == (
        "find_official_number", "prepare_task", "update_task", "start_assisted_call", "get_call_status",
        "answer_pending_choice", "join_call", "return_to_agent", "get_case_result",
    )
    assert tuple(get_args(m.ToolName)) == m.TOOL_NAMES
    assert list(m.TOOL_IO) == list(m.TOOL_NAMES)
    assert set(m.TOOL_ERROR_CODES) == set(m.TOOL_NAMES)
    assert m.USER_ACTION_TOOLS == ("start_assisted_call", "answer_pending_choice", "join_call", "return_to_agent")
    assert m.ASSISTANT_FIXED_ACTIONS == ("reply_only", "ask_clarify", "present_task_card", "wait_for_call")


def test_tool_error_codes_match_spec_table() -> None:
    assert m.TOOL_ERROR_CODES["update_task"] == ("unknown_task", "forbidden_field", "task_locked", "invalid_option")
    # r3 R2-1（定稿 add_unknown_task）：start_assisted_call 也有 unknown_task，放第一位
    assert m.TOOL_ERROR_CODES["start_assisted_call"] == (
        "unknown_task", "start_not_confirmed", "card_version_mismatch", "task_not_ready", "call_in_progress", "unsupported",
    )
    # A-18（定稿 add_unknown_task）：这三个工具也有 unknown_task
    assert m.TOOL_ERROR_CODES["answer_pending_choice"] == ("unknown_task", "unknown_choice", "invalid_option")
    assert m.TOOL_ERROR_CODES["join_call"] == ("unknown_task", "handoff_not_ready")
    assert m.TOOL_ERROR_CODES["return_to_agent"] == ("unknown_task", "no_active_handoff")
    assert all(m.TOOL_ERROR_CODES[t][0] == "unknown_task" for t in m.TOOL_NAMES if t not in ("find_official_number", "prepare_task"))
    every = {c for codes in m.TOOL_ERROR_CODES.values() for c in codes}
    assert every == set(get_args(m.ErrorCode))


@pytest.mark.parametrize("name", m.TOOL_NAMES)
def test_tool_output_schema_is_an_object(name: str) -> None:
    _, out = m.TOOL_IO[name]
    schema = out.model_json_schema()
    assert schema["type"] == "object"
    assert schema.get("additionalProperties") is False


@pytest.mark.parametrize(
    "alias, values",
    [
        (m.CallPhase, {"idle", "dialing", "ivr", "hold", "ai_agent", "live_rep", "awaiting_user",
                       "handoff_ready", "in_handoff", "resuming", "wrapping_up", "ended"}),
        (m.TaskStatus, {"setup_required", "ready", "in_call", "completed", "needs_follow_up", "failed", "unsupported"}),
        (m.PrepareStatus, {"ready", "setup_required", "unsupported"}),
        (m.CaseOutcome, {"in_progress", "confirmed", "needs_follow_up", "failed"}),
        (m.Action, {"press", "say", "release", "ask_user", "handoff", "end", "wait"}),
        (m.Mode, {"live-sim", "text-sim", "replay"}),
        (m.JevCallPhase, {"ivr_menu", "hold", "ai_agent", "live_rep", "identity_check", "ended"}),
        (m.PendingStatus, {"open", "answered", "expired"}),
        (m.JevQuestionType, {"choice", "noul", "score"}),
        (m.DecisionPoint, {"assistant_next_action", "company_skill", "task_intent", "precall_default",
                           "call_round", "said_as_decided", "call_outcome", "result_item_kind", "agent_tool"}),
        (m.TraceEventType, {
            "call_started", "call_ended", "hangup", "remote_hangup", "company_turn", "waitless_utterance",
            "decision", "action", "dtmf_sent", "injection_sent", "guard_released", "guard_blocked",
            "say_mismatch", "pending_choice_created", "user_answered", "pending_expired", "handoff_announced",
            "handoff_ready", "handoff_started", "handoff_ended", "resume_injected", "user_action", "error",
            "agent_tool_call", "agent_tool_result", "status_updated",
        }),
    ],
    ids=["CallPhase", "TaskStatus", "PrepareStatus", "CaseOutcome", "Action", "Mode", "JevCallPhase",
         "PendingStatus", "JevQuestionType", "DecisionPoint", "TraceEventType"],
)
def test_enum_values_match_spec(alias: Any, values: set[str]) -> None:
    assert set(get_args(alias)) == values


def test_decision_point_codes_and_call_round_questions() -> None:
    assert m.DECISION_POINT_CODES == {
        "assistant_next_action": "DP-A1", "company_skill": "DP-P1", "task_intent": "DP-P2",
        "precall_default": "DP-P3", "call_round": "DP-C1", "said_as_decided": "DP-C2",
        "call_outcome": "DP-E1", "result_item_kind": "DP-E2",
    }
    assert m.CALL_ROUND_QUESTIONS == {"menu_choice": "choice", "call_phase": "choice", "needs_user": "noul", "stakes": "score"}


def test_consequential_events_and_field_constants() -> None:
    assert m.CONSEQUENTIAL_EVENTS == {"dtmf_sent", "injection_sent", "pending_choice_created", "handoff_started", "hangup"}
    assert set(m.FORBIDDEN_FIELD_KEYS) == {"ssn", "card_number", "date_of_birth"}
    assert m.UPDATABLE_FIELD_KEYS == ("name", "phone_number", "address", "account_number")
    assert m.WS_CLOSE_UNAUTHORIZED == 4401
    assert m.HANDOFF_FRAME_BYTES == 640 and m.HANDOFF_SAMPLE_RATE == 16000


# ---------------------------------------------------------------------------
# 必填与多余字段
# ---------------------------------------------------------------------------

REQUIRED_CASES = [
    (m.TaskCard, task_card, ["card_version", "company", "goal", "effective_date", "precall_decisions", "boundary"]),
    (m.PendingChoice, pending, ["choice_id", "question_text", "options", "created_at", "expires_at", "status"]),
    (m.CallStatusOut, call_status, ["task_id", "call_id", "phase", "pending_choice", "handoff", "ended", "debug"]),
    (m.JoinCallOut, join_out, ["handoff_id", "ws_path", "token", "expires_at", "media"]),
    (m.CaseResultOut, case_result, ["outcome", "verified", "follow_ups", "confirmation_number", "calls", "history"]),
    (m.DecisionResult, decision_result, ["decision_id", "point", "intent", "action", "reason_code", "policy_rule", "jev"]),
    (m.CompanyTurn, company_turn, ["call_id", "seq", "t_start_ms", "t_end_ms", "text", "kind"]),
]


@pytest.mark.parametrize(
    "model, build, field",
    [(mod, b, f) for mod, b, fields in REQUIRED_CASES for f in fields],
    ids=lambda v: v if isinstance(v, str) else getattr(v, "__name__", "x"),
)
def test_required_field_missing_is_rejected(model: type[BaseModel], build: Any, field: str) -> None:
    data = build()
    model.model_validate(data)
    del data[field]
    with pytest.raises(ValidationError):
        model.model_validate(data)


@pytest.mark.parametrize("model, build", [(mod, b) for mod, b, _ in REQUIRED_CASES], ids=lambda v: getattr(v, "__name__", "x"))
def test_unknown_field_is_rejected(model: type[BaseModel], build: Any) -> None:
    with pytest.raises(ValidationError):
        model.model_validate({**build(), "surprise": 1})


def test_nullable_fields_accept_null() -> None:
    m.TaskCard.model_validate(task_card(effective_date=None))
    m.CallStatusOut.model_validate(call_status(call_id=None, pending_choice=None, debug=None))
    m.CaseResultOut.model_validate(case_result(outcome="needs_follow_up", confirmation_number=None))
    m.OfficialNumberOut.model_validate(
        {"company_id": None, "company_name": "X", "official_number": None, "verified": False, "sources": [], "reason": "no sources"}
    )


def test_update_task_fields_pass_unknown_keys_through_for_forbidden_field_error() -> None:
    fields = m.UpdateTaskFields.model_validate({"address": "1 Example St", "ssn": "900-00-0001"})
    assert fields.address == "1 Example St"
    assert fields.model_extra == {"ssn": "900-00-0001"}


# ---------------------------------------------------------------------------
# 边界
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("n, ok", [(0, False), (1, True), (100, True), (101, False)])
def test_find_official_number_company_length(n: int, ok: bool) -> None:
    data = {"company": "x" * n, "intent": "start service"}
    if ok:
        m.FindOfficialNumberIn.model_validate(data)
    else:
        with pytest.raises(ValidationError):
            m.FindOfficialNumberIn.model_validate(data)


@pytest.mark.parametrize("n, ok", [(0, False), (200, True), (201, False)])
def test_find_official_number_intent_length(n: int, ok: bool) -> None:
    data = {"company": "Lakeshore Energy", "intent": "y" * n}
    if ok:
        m.FindOfficialNumberIn.model_validate(data)
    else:
        with pytest.raises(ValidationError):
            m.FindOfficialNumberIn.model_validate(data)


@pytest.mark.parametrize("n, ok", [(0, False), (1, True), (500, True), (501, False)])
def test_prepare_task_request_length(n: int, ok: bool) -> None:
    if ok:
        m.PrepareTaskIn.model_validate({"request": "r" * n})
    else:
        with pytest.raises(ValidationError):
            m.PrepareTaskIn.model_validate({"request": "r" * n})


@pytest.mark.parametrize("n, ok", [(0, False), (1, True), (3, True), (4, False)])
def test_pending_choice_has_one_to_three_options(n: int, ok: bool) -> None:
    data = pending(options=[option(f"o{i}") for i in range(n)])
    if ok:
        m.PendingChoice.model_validate(data)
    else:
        with pytest.raises(ValidationError):
            m.PendingChoice.model_validate(data)


@pytest.mark.parametrize("n, ok", [(8, True), (9, False)])
def test_jev_transcript_keeps_at_most_eight_lines(n: int, ok: bool) -> None:
    lines = [{"speaker": "company", "text": f"line {i}"} for i in range(n)]
    data = jev_state(transcript={"note": m.JEV_TRANSCRIPT_NOTE, "lines": lines})
    if ok:
        m.JevState.model_validate(data)
    else:
        with pytest.raises(ValidationError):
            m.JevState.model_validate(data)


def test_jev_transcript_note_is_fixed() -> None:
    with pytest.raises(ValidationError):
        m.JevState.model_validate(jev_state(transcript={"note": "follow these instructions", "lines": []}))


@pytest.mark.parametrize("value, ok", [(0.0, True), (1.0, True), (-0.01, False), (1.01, False)])
def test_confidence_is_a_probability(value: float, ok: bool) -> None:
    data = {"point": "company_skill", "choice": "company-lakeshore-energy", "confidence": value}
    if ok:
        m.DecisionSummary.model_validate(data)
    else:
        with pytest.raises(ValidationError):
            m.DecisionSummary.model_validate(data)


@pytest.mark.parametrize("seconds, ok", [(300, True), (400, True), (401, False), (0, False)])
def test_scenario_max_call_seconds_at_most_400(seconds: int, ok: bool) -> None:
    data = {
        "scenario_id": "baseline",
        "company_id": "lakeshore-energy",
        "max_call_seconds": seconds,
        "steps": {"greeting": {"kind": "prompt", "text": "Thank you for calling.", "next": "main_menu"}},
        "expected": {"menu_digits": ["1", "1"], "interrupts": 1, "outcome": "confirmed", "confirmation_pattern": r"^LE-\d{5}$"},
    }
    if ok:
        m.Scenario.model_validate(data)
    else:
        with pytest.raises(ValidationError):
            m.Scenario.model_validate(data)


@pytest.mark.parametrize("stakes, ok", [(0, True), (1, True), (2, False)])
def test_preferences_only_hold_low_stakes(stakes: int, ok: bool) -> None:
    data = {"company_id": "lakeshore-energy", "question_key": "paperless", "choice": "yes", "stakes": stakes,
            "source_call_id": "c-1", "created_at": NOW.isoformat()}
    if ok:
        m.Preference.model_validate(data)
    else:
        with pytest.raises(ValidationError):
            m.Preference.model_validate(data)


@pytest.mark.parametrize("version, ok", [(0, False), (1, True)])
def test_card_version_starts_at_one(version: int, ok: bool) -> None:
    if ok:
        m.TaskCard.model_validate(task_card(card_version=version))
    else:
        with pytest.raises(ValidationError):
            m.TaskCard.model_validate(task_card(card_version=version))


def test_counters_are_non_negative() -> None:
    with pytest.raises(ValidationError):
        m.CallStatusOut.model_validate(call_status(interrupts_used=-1))
    with pytest.raises(ValidationError):
        m.CallStatusOut.model_validate(call_status(elapsed_s=-0.5))


# ---------------------------------------------------------------------------
# 严格类型与字面量
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("confirmed", ["true", 1, "yes", None])
def test_start_confirmed_must_be_a_real_boolean(confirmed: Any) -> None:
    with pytest.raises(ValidationError):
        m.StartCallIn.model_validate({"task_id": "t-1", "confirmed": confirmed, "card_version": 1})


@pytest.mark.parametrize("version", ["1", 1.5, True])
def test_start_card_version_must_be_an_integer(version: Any) -> None:
    with pytest.raises(ValidationError):
        m.StartCallIn.model_validate({"task_id": "t-1", "confirmed": True, "card_version": version})


def test_start_accepts_strict_values() -> None:
    m.StartCallIn.model_validate({"task_id": "t-1", "confirmed": False, "card_version": 2})


def test_media_recording_is_always_false() -> None:
    with pytest.raises(ValidationError):
        m.Media.model_validate({"ai_media": "connected", "recording": True, "transcription": True})
    with pytest.raises(ValidationError):
        m.JoinMedia.model_validate({"ai_media": "disconnected", "recording": False, "transcription": True})
    with pytest.raises(ValidationError):
        m.ReturnMedia.model_validate({"ai_media": "disconnected", "recording": False, "transcription": True})
    m.JoinMedia.model_validate(media("disconnected", False))
    m.ReturnMedia.model_validate(media("connected", True))


def test_handoff_ws_frames() -> None:
    m.HandoffHello.model_validate({"type": "hello", "format": "pcm_s16le", "sample_rate": 16000, "channels": 1})
    with pytest.raises(ValidationError):
        m.HandoffHello.model_validate({"type": "hello", "format": "pcm_s16le", "sample_rate": 48000, "channels": 1})
    m.HandoffEnded.model_validate({"type": "ended", "reason": "company_hangup"})
    with pytest.raises(ValidationError):
        m.HandoffEnded.model_validate({"type": "ended", "reason": "timeout"})


# ---------------------------------------------------------------------------
# 跨字段规则
# ---------------------------------------------------------------------------


def test_join_ws_path_names_the_handoff() -> None:
    with pytest.raises(ValidationError):
        m.JoinCallOut.model_validate(join_out(ws_path="/ws/handoff/other"))


def test_no_transcript_tail_while_in_handoff() -> None:
    with pytest.raises(ValidationError):
        m.CallStatusOut.model_validate(call_status(phase="in_handoff"))
    m.CallStatusOut.model_validate(call_status(phase="in_handoff", transcript_tail=[]))


@pytest.mark.parametrize("tool", m.USER_ACTION_TOOLS)
def test_next_never_carries_a_user_action_tool(tool: str) -> None:
    nxt = {"kind": "tool_call", "tool_use_id": "u-1", "name": tool, "arguments": {"task_id": "t-1"}}
    with pytest.raises(ValidationError):
        m.AssistantTurnOut.model_validate(assistant_out(nxt))


@pytest.mark.parametrize("tool", sorted(set(m.TOOL_NAMES) - set(m.USER_ACTION_TOOLS)))
def test_next_may_carry_other_tools(tool: str) -> None:
    out = m.AssistantTurnOut.model_validate(
        assistant_out({"kind": "tool_call", "tool_use_id": "u-1", "name": tool, "arguments": {}})
    )
    assert out.next.kind == "tool_call"


def test_next_rejects_unknown_tool_and_unknown_kind() -> None:
    with pytest.raises(ValidationError):
        m.AssistantTurnOut.model_validate(assistant_out({"kind": "tool_call", "tool_use_id": "u", "name": "dial_now", "arguments": {}}))
    with pytest.raises(ValidationError):
        m.AssistantTurnOut.model_validate(assistant_out({"kind": "run_shell"}))


def test_next_is_discriminated_by_kind() -> None:
    assert isinstance(m.AssistantTurnOut.model_validate(assistant_out({"kind": "poll", "task_id": "t-1"})).next, m.NextPoll)
    assert isinstance(
        m.AssistantTurnOut.model_validate(assistant_out({"kind": "await_user", "ui": "task_card"})).next, m.NextAwaitUser
    )
    with pytest.raises(ValidationError):
        m.AssistantTurnOut.model_validate(assistant_out({"kind": "await_user", "ui": "anything"}))


def test_assistant_turn_needs_some_input() -> None:
    with pytest.raises(ValidationError):
        m.AssistantTurnIn.model_validate({"conversation_id": "c", "tools": []})
    m.AssistantTurnIn.model_validate({"conversation_id": "c", "tools": [], "event": "call_ended", "task_id": "t-1"})


def test_decision_result_action_payloads() -> None:
    with pytest.raises(ValidationError):
        m.DecisionResult.model_validate(decision_result(digits=None))
    with pytest.raises(ValidationError):
        m.DecisionResult.model_validate(decision_result(action="say", digits=None))
    with pytest.raises(ValidationError):
        m.DecisionResult.model_validate(decision_result(action="release"))
    with pytest.raises(ValidationError):
        m.DecisionResult.model_validate(decision_result(digits="1a"))
    m.DecisionResult.model_validate(decision_result(action="say", digits=None, say_text="Paperless billing, please."))
    m.DecisionResult.model_validate(decision_result(digits="0123456789*#"))


def test_decision_result_call_phase_uses_jev_criteria_ids() -> None:
    m.DecisionResult.model_validate(decision_result(call_phase="identity_check"))
    with pytest.raises(ValidationError):
        m.DecisionResult.model_validate(decision_result(call_phase="in_handoff"))


@pytest.mark.parametrize("event_type", sorted(m.CONSEQUENTIAL_EVENTS))
def test_a25_consequential_rows_without_decision_id_still_read_back(event_type: str) -> None:
    # A-25（定稿 keep_all_relax_trace_event）：写入检查在 Trace.event()，读回的坏行要能构造，check_call 才能报 Violation
    ev = m.TraceEvent.model_validate({"task_id": "t-1", "call_id": "c-1", "event_type": event_type})
    assert ev.decision_id is None
    m.TraceEvent.model_validate({"task_id": "t-1", "call_id": "c-1", "event_type": event_type, "decision_id": "d-1"})


def test_a25_incomplete_decision_rows_still_read_back() -> None:
    base = {"task_id": "t-1", "event_type": "decision", "decision_id": "d-1", "decision_point": "call_round",
            "jev_request": {"q": 1}, "jev_response": {"a": 1}, "policy_decision": {"rule": "menu"},
            "action": {"turn_seq": 3}}
    m.TraceEvent.model_validate(base)
    for missing in m.DECISION_EVENT_REQUIRED:
        ev = m.TraceEvent.model_validate({k: v for k, v in base.items() if k != missing})
        assert getattr(ev, missing) is None


def test_a25_a08_write_check_constants() -> None:
    assert m.DECISION_EVENT_REQUIRED == ("decision_id", "decision_point", "jev_request", "jev_response", "policy_decision")
    assert m.TRACE_ACTION_KEYS == {
        "company_turn": ("kind", "seq"),
        "dtmf_sent": ("digits",),
        "injection_sent": ("text", "template"),
        "user_action": ("name", "shown_ms", "acted_ms"),
        "decision": ("turn_seq",),
        "handoff_ended": ("dropped_sonic_outputs",),  # r4 D-G-4b
        "agent_tool_call": ("tool", "tool_call_id", "args"),
        "agent_tool_result": ("tool", "tool_call_id", "ok", "result", "latency_ms"),
        "status_updated": ("phase", "status_line"),
    }
    assert m.TURN_SEQ_KEY == "turn_seq"


def test_transcript_trace_events_name_the_speaker() -> None:
    with pytest.raises(ValidationError):
        m.TraceEvent.model_validate({"task_id": "t", "event_type": "company_turn", "speaker": "waitless", "transcript": "x"})
    m.TraceEvent.model_validate({"task_id": "t", "event_type": "company_turn", "speaker": "company", "transcript": "x"})
    with pytest.raises(ValidationError):
        m.TraceEvent.model_validate({"task_id": "t", "event_type": "waitless_utterance", "speaker": "waitless"})


@pytest.mark.parametrize("key", m.FORBIDDEN_FIELD_KEYS)
def test_forbidden_keys_never_reach_models_or_storage(key: str) -> None:
    with pytest.raises(ValidationError):
        m.JevState.model_validate(jev_state(user_profile={"name": "Alex", key: "x"}))
    with pytest.raises(ValidationError):
        m.Profile.model_validate({"name": "Alex", key: "x"})
    ctx = {"task_card": None, "allowed_fields": {key: "x"}, "standing_rules": [], "prior_choices": [],
           "interrupts_used": 0, "phase": "ivr"}
    with pytest.raises(ValidationError):
        m.DecisionContext.model_validate(ctx)


@pytest.mark.parametrize("commit, ok", [(HEX40, True), ("unknown", True), ("abc123", False), (HEX40.upper(), False)])
def test_skill_commit_is_40_hex_or_unknown(commit: str, ok: bool) -> None:
    data = {"commit": commit, "content_sha256": HEX64, "dirty": False}
    if ok:
        m.SkillVersion.model_validate(data)
    else:
        with pytest.raises(ValidationError):
            m.SkillVersion.model_validate(data)


@pytest.mark.parametrize("name, ok", [("decide-option", True), ("Decide-Option", False), ("a" * 64, True), ("a" * 65, False)])
def test_skill_name_rules(name: str, ok: bool) -> None:
    data = {"name": name, "description": "d", "metadata": {}, "body": "", "path": "skills/x", "files": [],
            "version": {"commit": "unknown", "content_sha256": HEX64, "dirty": False}}
    if ok:
        m.Skill.model_validate(data)
    else:
        with pytest.raises(ValidationError):
            m.Skill.model_validate(data)


def test_precall_default_and_selected_must_be_options() -> None:
    card = task_card()
    card["precall_decisions"][0]["default"] = "cash"
    with pytest.raises(ValidationError):
        m.TaskCard.model_validate(card)
    card = task_card()
    card["precall_decisions"][0].update(selected=None, needs_user=True)
    m.TaskCard.model_validate(card)


def test_pending_choice_window_and_unique_options() -> None:
    with pytest.raises(ValidationError):
        m.PendingChoice.model_validate(pending(expires_at=NOW.isoformat()))
    with pytest.raises(ValidationError):
        m.PendingChoice.model_validate(pending(options=[option("a"), option("a")]))
    with pytest.raises(ValidationError):
        m.PendingChoice.model_validate(pending(created_at="2026-10-14T12:00:00"))  # 没有时区


def test_company_turn_kind_matches_menu_options() -> None:
    with pytest.raises(ValidationError):
        m.CompanyTurn.model_validate(company_turn(menu_options=[]))
    with pytest.raises(ValidationError):
        m.CompanyTurn.model_validate(company_turn(kind="speech"))
    with pytest.raises(ValidationError):
        m.CompanyTurn.model_validate(company_turn(t_end_ms=10))
    m.CompanyTurn.model_validate(company_turn(kind="speech", menu_options=[], text="What is the service address?"))


def test_jev_question_criteria_follow_type() -> None:
    m.JevQuestion.model_validate({"type": "choice", "instructions": "Pick", "criteria": {"a": "A"}})
    m.JevQuestion.model_validate({"type": "score", "instructions": "Rate", "criteria": ["0: none", "1: minor"]})
    m.JevQuestion.model_validate({"type": "noul", "instructions": "Is it?"})
    for bad in (
        {"type": "choice", "instructions": "Pick"},
        {"type": "choice", "instructions": "Pick", "criteria": ["a"]},
        {"type": "score", "instructions": "Rate", "criteria": {"0": "none"}},
        {"type": "noul", "instructions": "Is it?", "criteria": {"y": "yes"}},
        {"type": "text", "instructions": "Write a reply"},
        {"type": "noul", "instructions": ""},
    ):
        with pytest.raises(ValidationError):
            m.JevQuestion.model_validate(bad)


def test_jev_answer_fields_follow_type() -> None:
    m.JevAnswer.model_validate({"type": "choice", "choice": "1", "confidence": 0.9, "probabilities": {"1": 0.9}})
    m.JevAnswer.model_validate({"type": "noul", "noul": 0.4, "extra_from_api": True})
    for bad in (
        {"type": "choice", "choice": "1", "confidence": 0.9},
        {"type": "noul"},
        {"type": "score", "confidence": 0.9},
        {"type": "noul", "noul": 1.5},
    ):
        with pytest.raises(ValidationError):
            m.JevAnswer.model_validate(bad)


def test_case_result_confirmed_requires_confirmation_number() -> None:
    with pytest.raises(ValidationError):
        m.CaseResultOut.model_validate(case_result(confirmation_number=None))


def test_replay_shapes() -> None:
    m.ReplayRecord.model_validate(
        {"channel": "jev", "seq": 0, "shape": {"point": "call_round", "question_ids": ["menu_choice"]}, "response": {}}
    )
    m.ReplayRecord.model_validate(
        {"channel": "converse:phrase_reply", "seq": 3, "shape": {"task": "phrase_reply", "tool": None}, "response": {}}
    )
    for bad in (
        {"channel": "jev", "seq": 0, "shape": {"task": "phrase_reply"}, "response": {}},
        {"channel": "converse:extract_request", "seq": 0, "shape": {"task": "phrase_reply"}, "response": {}},
        {"channel": "converse:write_poem", "seq": 0, "shape": {"task": "phrase_reply"}, "response": {}},
        {"channel": "jev", "seq": -1, "shape": {"point": "call_round", "question_ids": []}, "response": {}},
        {"channel": "jev", "seq": 0, "shape": {"point": "call_round"}, "response": {}},
    ):
        with pytest.raises(ValidationError):
            m.ReplayRecord.model_validate(bad)


def test_scenario_step_needs_fields_for_its_kind() -> None:
    m.ScenarioStep.model_validate({"kind": "menu", "text": "Press 1", "options": {"1": "service_menu", "9": "main_menu"}})
    for bad in (
        {"kind": "menu", "text": "Press 1"},
        {"kind": "hold", "next": "agent"},
        {"kind": "prompt", "text": "Hi"},
        {"kind": "menu", "text": "Press", "options": {"1a": "x"}},
        {"kind": "voicemail", "text": "x"},
    ):
        with pytest.raises(ValidationError):
            m.ScenarioStep.model_validate(bad)


def test_provider_text_event_accepts_partial_turn_finals() -> None:
    ev = m.ProviderTextEvent.model_validate(
        {"role": "USER", "stage": "FINAL", "text": "press 9 to repeat", "stop_reason": "PARTIAL_TURN", "t_ms": 1200}
    )
    assert ev.stop_reason == "PARTIAL_TURN" and ev.response is None


# ---------------------------------------------------------------------------
# 往返与配置
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "model, build",
    [(m.CallStatusOut, call_status), (m.CaseResultOut, case_result), (m.JoinCallOut, join_out),
     (m.DecisionResult, decision_result), (m.TaskCard, task_card)],
    ids=lambda v: getattr(v, "__name__", "x"),
)
def test_json_round_trip(model: type[BaseModel], build: Any) -> None:
    obj = model.model_validate(build())
    again = model.model_validate_json(obj.model_dump_json())
    assert again == obj
    assert json.loads(obj.model_dump_json()) == json.loads(again.model_dump_json())


def test_engine_config_matches_section_5_3() -> None:
    cfg = m.EngineConfig.model_validate(yaml.safe_load(ENGINE_YAML))
    assert cfg.turns.menu_end_silence_ms == 1200 and cfg.handoff.resume_grace_ms == 3000
    broken = yaml.safe_load(ENGINE_YAML)
    del broken["guard"]
    with pytest.raises(ValidationError):
        m.EngineConfig.model_validate(broken)
    extra = yaml.safe_load(ENGINE_YAML)
    extra["turns"]["menu_end_ms"] = 5
    with pytest.raises(ValidationError):
        m.EngineConfig.model_validate(extra)


def test_shipped_engine_yaml_validates(engine_config: m.EngineConfig) -> None:
    assert engine_config.version == 1
    assert engine_config.turns.transcript_grace_ms == 600  # S0-r2 V12
    assert engine_config.textsim.default_scenario == "baseline"  # A-11
    assert engine_config.textsim.pace_ms == 0  # A-20：演示时由环境变量覆盖，文件里保持 0


def test_settings_moved_out_of_shared_models() -> None:
    # A-27（定稿 split_engine_frozen）：EngineConfig 留在 models，Settings 与 SETTINGS_ENV 归 A 线的 config.py
    assert not hasattr(m, "Settings") and not hasattr(m, "SETTINGS_ENV")
    assert hasattr(m, "EngineConfig")


def test_settings_env_names_cover_section_5_2() -> None:
    from waitless import config as c

    # A 线可以增补变量（A-27），所以这里只要求覆盖 §5.2 全部名字与 A-20 的覆盖变量
    assert set(c.SETTINGS_ENV) >= {
        "WAITLESS_MODE", "WAITLESS_HOST", "WAITLESS_PORT", "WAITLESS_ALLOWED_HOSTS", "WAITLESS_PUBLIC_BASE_URL",
        "WAITLESS_DB_PATH", "WAITLESS_WEB_DIR", "WAITLESS_SKILLS_DIR", "WAITLESS_COMPANY_DIR", "WAITLESS_ENGINE_CONFIG",
        "WAITLESS_CARRIER_URL", "WAITLESS_LINE_WS_URL", "WAITLESS_COMPANY_HOST", "WAITLESS_COMPANY_PORT",
        "AWS_REGION", "AWS_ACCESS_KEY_ID", "AWS_SECRET_ACCESS_KEY", "WAITLESS_SONIC_MODEL_ID", "WAITLESS_SONIC_VOICE",
        "WAITLESS_PLANNER_MODEL_ID", "WAITLESS_JEV_URL", "WAITLESS_JEV_MODEL", "WAITLESS_JEV_API_KEY",
        "WAITLESS_REPLAY_DIR", "WAITLESS_RECORD_DIR", "WAITLESS_TEXTSIM_PACE_MS",
    }
    assert set(c.SETTINGS_ENV.values()) <= set(c.Settings.model_fields)
    assert c.SETTINGS_ENV["WAITLESS_TEXTSIM_PACE_MS"] == "textsim_pace_ms"
    # A-13：company 进程的两个变量单列，不属于任何模式的必需项
    assert c.REQUIRED_FOR_COMPANY == ("WAITLESS_COMPANY_HOST", "WAITLESS_COMPANY_PORT")


def test_settings_never_print_secrets() -> None:
    from waitless import config as c

    s = c.Settings(
        mode="text-sim", host="127.0.0.1", port=0, allowed_hosts=("127.0.0.1",),
        db_path="w.db", web_dir="web", skills_dir="skills", company_dir="company",
        engine_config_path="server/config/engine.yaml",
        jev_api_key=SecretStr("sentinel-value-123"), aws_secret_access_key=SecretStr("sentinel-value-456"),
        engine=yaml.safe_load(ENGINE_YAML),
    )
    shown = repr(s) + str(s) + s.model_dump_json()
    assert "sentinel-value-123" not in shown and "sentinel-value-456" not in shown
    assert s.jev_api_key is not None and s.jev_api_key.get_secret_value() == "sentinel-value-123"
    with pytest.raises(ValidationError):
        c.Settings.model_validate({**s.model_dump(), "mode": "demo"})
    with pytest.raises(ValidationError):
        c.Settings.model_validate({**s.model_dump(), "textsim_pace_ms": -1})


# ---------------------------------------------------------------------------
# r2：骨架待定写法定稿后的模型规则（RUN/step3/ambiguity-decisions.md）
# ---------------------------------------------------------------------------


def decision_context(**over: Any) -> dict[str, Any]:
    d = {"task_card": None, "allowed_fields": {"address": "1200 W Example St"}, "standing_rules": [],
         "prior_choices": [], "interrupts_used": 0, "phase": "live_rep"}
    d.update(over)
    return d


def decision_request(**over: Any) -> dict[str, Any]:
    d = {
        "decision_id": "d-9",
        "point": "said_as_decided",
        "call_id": "c-1",
        "task_id": "t-1",
        "turn": None,
        "candidates": [],
        "context": decision_context(),
        "transcript": [{"speaker": "waitless", "text": "We will pay the deposit."}],
        "template_vars": {m.SAID_SUMMARY_VAR: "We will pay the deposit."},
    }
    d.update(over)
    return d


def prepare_out(**over: Any) -> dict[str, Any]:
    d = {"task_id": "t-1", "status": "ready", "task_card": task_card(), "missing": [], "skills": [],
         "decisions": [], "message": "Ready to call."}
    d.update(over)
    return d


def call_result(**over: Any) -> dict[str, Any]:
    d = {
        "call_id": "c-1",
        "outcome": "confirmed",
        "confirmation_number": "LE-12345",
        "verified": [{"label": "Confirmation", "value": "LE-12345", "evidence": {"call_id": "c-1", "turn_seq": 40}}],
        "follow_ups": [],
        "summary": "Service is scheduled.",
    }
    d.update(over)
    return d


def company_config(**over: Any) -> dict[str, Any]:
    d = {
        "company_id": "lakeshore-energy",
        "name": "Lakeshore Energy",
        "line_number": "(312) 555-0100",
        "website": {"domain": "lakeshore-energy.example", "dir": "www"},
        "voices": {"ivr_polly": "voice-a", "agent_sonic": "voice-b", "rep_sonic": "voice-c"},
        "customers": [],
        "menu": {"timeout_sec": 8, "max_timeouts": 2},
        "dead_end_text": "This option is not available. Goodbye.",
    }
    d.update(over)
    return d


def scenario(**over: Any) -> dict[str, Any]:
    d = {
        "scenario_id": "baseline",
        "company_id": "lakeshore-energy",
        "max_call_seconds": 300,
        "steps": {
            "greeting": {"kind": "prompt", "text": "Thank you for calling.", "next": "main_menu"},
            "main_menu": {"kind": "menu", "text": "Press 1 or 2.", "options": {"1": "greeting", "2": m.DEAD_END_TARGET}},
        },
        "expected": {"menu_digits": ["1"], "interrupts": 0, "outcome": "failed", "confirmation_pattern": r"^LE-\d{5}$"},
    }
    d.update(over)
    return d


# --- A-02 wait_except_c2 ------------------------------------------------------


def test_a02_constants() -> None:
    assert m.WAIT_ONLY_POINTS == ("assistant_next_action", "company_skill", "task_intent", "precall_default",
                                  "call_outcome", "result_item_kind")
    assert set(m.WAIT_ONLY_POINTS) | {"call_round", "said_as_decided", "agent_tool"} == set(get_args(m.DecisionPoint))
    assert m.SAID_INTENTS == ("said_ok", "reinject", "safe_default")
    assert m.SAY_MISMATCH == "say_mismatch" and m.SAID_SUMMARY_VAR == "decision_summary"


@pytest.mark.parametrize("point", m.WAIT_ONLY_POINTS)
def test_a02_non_call_points_always_wait(point: str) -> None:
    ok = decision_result(point=point, intent="company-lakeshore-energy", action="wait", digits=None)
    m.DecisionResult.model_validate(ok)
    for action, extra in (("press", {"digits": "1"}), ("say", {"say_text": "Hello"}), ("end", {}), ("ask_user", {})):
        bad = decision_result(point=point, intent="x", action=action, digits=None)
        bad.update(extra)
        with pytest.raises(ValidationError):
            m.DecisionResult.model_validate(bad)


@pytest.mark.parametrize(
    "intent, action, extra, ok",
    [
        ("said_ok", "wait", {}, True),
        ("said_ok", "say", {"say_text": "again"}, False),
        ("reinject", "say", {"say_text": "We will pay the deposit."}, True),
        ("reinject", "wait", {}, False),
        ("safe_default", "end", {"reason_code": "say_mismatch"}, True),
        ("safe_default", "end", {"reason_code": "low_conf"}, False),
        ("retry", "say", {"say_text": "x"}, False),
    ],
    ids=["ok-wait", "ok-say", "reinject-say", "reinject-wait", "safe-end", "safe-wrong-reason", "unknown-intent"],
)
def test_a02_said_as_decided_results(intent: str, action: str, extra: dict[str, Any], ok: bool) -> None:
    data = decision_result(point="said_as_decided", intent=intent, action=action, digits=None)
    data.update(extra)
    if ok:
        m.DecisionResult.model_validate(data)
    else:
        with pytest.raises(ValidationError):
            m.DecisionResult.model_validate(data)


def test_a02_call_round_results_keep_all_actions() -> None:
    m.DecisionResult.model_validate(decision_result(intent="ask_user", action="ask_user", digits=None))
    m.DecisionResult.model_validate(decision_result(intent="confirm_and_end", action="end", digits=None))


def test_a02_attempt_defaults_to_zero_and_counts_c2_only() -> None:
    req = m.DecisionRequest.model_validate(decision_request())
    assert req.attempt == 0
    assert m.DecisionRequest.model_validate(decision_request(attempt=1)).attempt == 1
    with pytest.raises(ValidationError):
        m.DecisionRequest.model_validate(decision_request(attempt=-1))
    planning = decision_request(point="company_skill", call_id=None, transcript=[{"speaker": "user", "text": "Call my power company"}],
                                template_vars={})
    m.DecisionRequest.model_validate(planning)
    with pytest.raises(ValidationError):
        m.DecisionRequest.model_validate({**planning, "attempt": 1})


@pytest.mark.parametrize(
    "over",
    [{"call_id": None}, {"transcript": []}, {"template_vars": {}}, {"template_vars": {m.SAID_SUMMARY_VAR: ""}}],
    ids=["no-call-id", "no-transcript", "no-summary", "empty-summary"],
)
def test_a02_said_as_decided_request_needs_call_utterance_and_summary(over: dict[str, Any]) -> None:
    with pytest.raises(ValidationError):
        m.DecisionRequest.model_validate(decision_request(**over))


def test_a01_round_requests_still_need_turn_and_call() -> None:
    base = decision_request(point="call_round", template_vars={}, turn=company_turn())
    m.DecisionRequest.model_validate(base)
    with pytest.raises(ValidationError):
        m.DecisionRequest.model_validate({**base, "turn": None})


# --- A-03 prior_choices_source ------------------------------------------------


def test_a03_prior_choice_from_preference() -> None:
    pref = m.Preference.model_validate({"company_id": "lakeshore-energy", "question_key": "paperless", "choice": "yes",
                                        "stakes": 1, "source_call_id": "c-1", "created_at": NOW.isoformat()})
    pc = m.PriorChoice.from_preference(pref)
    assert (pc.question_key, pc.choice, pc.stakes, pc.source) == ("paperless", "yes", 1, "preference")
    m.DecisionContext.model_validate(decision_context(prior_choices=[pc.model_dump()]))


# --- A-04 map_by_company ------------------------------------------------------


def test_a04_profile_accounts_map_company_to_number() -> None:
    p = m.Profile.model_validate({"name": "Alex Example", "accounts": {"lakeshore-energy": "100200300"}})
    assert p.accounts == {"lakeshore-energy": "100200300"}
    assert m.Profile().accounts == {}
    for bad in ({"": "1"}, {"lakeshore-energy": ""}, {"lakeshore-energy": ["1", "2"]}):
        with pytest.raises(ValidationError):
            m.Profile.model_validate({"accounts": bad})


# --- A-05 allow_extra_reject_unknown -------------------------------------------


def test_a05_update_fields_schema_lets_unknown_keys_through() -> None:
    schema = m.UpdateTaskFields.model_json_schema()
    assert schema.get("additionalProperties") is True
    fields = m.UpdateTaskFields.model_validate({"adress": "typo", "card_number": "x"})
    assert fields.model_extra == {"adress": "typo", "card_number": "x"}


# --- A-09 task_latest_decision ------------------------------------------------


def test_a09_status_before_dialing_can_carry_a_planning_decision() -> None:
    debug = call_status()["debug"]
    debug["last_decision"].update(point="company_skill", intent="company-lakeshore-energy", action="wait")
    out = m.CallStatusOut.model_validate(call_status(call_id=None, phase="idle", transcript_tail=[], pending_choice=None,
                                                     interrupts_used=0, elapsed_s=0, debug=debug))
    assert out.debug is not None and out.debug.last_decision is not None
    debug["last_decision"] = None
    m.CallStatusOut.model_validate(call_status(call_id=None, phase="idle", pending_choice=None, debug=debug))


# --- A-10 nullable_get_before_dial --------------------------------------------


def test_a10_in_progress_call_summary_has_nulls() -> None:
    row = case_result()["calls"][0]
    row.update(scenario_id=None, ended_at=None, duration_s=None, outcome="in_progress")
    m.CaseResultOut.model_validate(case_result(outcome="in_progress", confirmation_number=None, calls=[row]))


# --- A-11 config_default_plus_replay_header -----------------------------------


def test_a11_engine_default_scenario_and_replay_header_scenario() -> None:
    broken = yaml.safe_load(ENGINE_YAML)
    del broken["textsim"]["default_scenario"]
    with pytest.raises(ValidationError):
        m.EngineConfig.model_validate(broken)
    header = {"replay_version": 1, "recorded_at": NOW.isoformat(), "source_mode": "text-sim", "scenario_id": "baseline",
              "request_text": "Start electric service on November 1.",
              "skills": [{"name": "decide-option", "commit": HEX40}], "models": {"jev": "j", "planner": "bedrock:p"}}
    assert m.ReplayHeader.model_validate(header).scenario_id == "baseline"
    for bad in ({k: v for k, v in header.items() if k != "scenario_id"}, {**header, "scenario_id": ""}):
        with pytest.raises(ValidationError):
            m.ReplayHeader.model_validate(bad)


# --- A-14 / A-19 status and task card -----------------------------------------


@pytest.mark.parametrize("model", [m.PrepareTaskOut, m.UpdateTaskOut], ids=["prepare", "update"])
@pytest.mark.parametrize(
    "status, card, missing, ok",
    [
        ("ready", True, [], True),
        ("ready", False, [], False),
        ("ready", True, [{"key": "address", "label": "Address"}], False),
        ("setup_required", True, [{"key": "address", "label": "Address"}], True),
        ("setup_required", False, [], True),  # A-14：号码未核实
        ("setup_required", False, [{"key": "address", "label": "Address"}], False),
        ("unsupported", False, [], True),
        ("unsupported", True, [], False),
    ],
    ids=["ready", "ready-no-card", "ready-missing", "setup-missing", "setup-unverified", "setup-null-card-missing",
         "unsupported", "unsupported-card"],
)
def test_a14_a19_status_card_rules(model: type[BaseModel], status: str, card: bool, missing: list[Any], ok: bool) -> None:
    data = prepare_out(status=status, task_card=task_card() if card else None, missing=missing)
    if model is m.UpdateTaskOut:
        data = {k: data[k] for k in ("task_id", "status", "task_card", "missing")}
    if ok:
        model.model_validate(data)
    else:
        with pytest.raises(ValidationError):
            model.model_validate(data)


@pytest.mark.parametrize("status", ["in_call", "completed", "needs_follow_up", "failed"])
def test_a19_update_task_status_uses_prepare_values(status: str) -> None:
    with pytest.raises(ValidationError):
        m.UpdateTaskOut.model_validate({"task_id": "t-1", "status": status, "task_card": task_card(), "missing": []})


# --- A-16 field_templates / A-17 reserved_hangup_target ------------------------


def test_a16_a17_constants() -> None:
    assert m.OPEN_ANSWER_TEMPLATE_PREFIX == "open_answer."
    assert m.FIELD_CUES_PREFIX == "field_cues."
    assert m.DEAD_END_TARGET == "dead_end"
    assert "ask_repeat" in m.SAY_TEMPLATE_KEYS


def test_a17_company_needs_dead_end_text() -> None:
    m.CompanyConfig.model_validate(company_config())
    for bad in ({k: v for k, v in company_config().items() if k != "dead_end_text"}, company_config(dead_end_text="")):
        with pytest.raises(ValidationError):
            m.CompanyConfig.model_validate(bad)


def test_a17_dead_end_is_a_target_not_a_step() -> None:
    m.Scenario.model_validate(scenario())
    steps = dict(scenario()["steps"])
    steps[m.DEAD_END_TARGET] = {"kind": "prompt", "text": "Goodbye.", "next": "greeting"}
    with pytest.raises(ValidationError):
        m.Scenario.model_validate(scenario(steps=steps))


# --- A-24 methods_callresult --------------------------------------------------


def test_a24_call_result_rules() -> None:
    m.CallResult.model_validate(call_result())
    m.CallResult.model_validate(call_result(outcome="needs_follow_up", confirmation_number=None, verified=[]))
    for bad in (
        call_result(confirmation_number=None),
        call_result(outcome="in_progress"),
        call_result(verified=[{"label": "x", "value": "y", "evidence": {"call_id": "c-2", "turn_seq": 1}}]),
    ):
        with pytest.raises(ValidationError):
            m.CallResult.model_validate(bad)


def test_a24_call_result_round_trip() -> None:
    obj = m.CallResult.model_validate(call_result())
    assert m.CallResult.model_validate_json(obj.model_dump_json()) == obj


# ---------------------------------------------------------------------------
# r3：骨架缺口定稿（RUN/step3/gap-decisions-r3.md）
# ---------------------------------------------------------------------------


def ivr_intent(**over: Any) -> dict[str, Any]:
    d = {
        "id": "start_service",
        "description": "Start electric service at a new address",
        "goal": "Start electric service on {effective_date}",
        "task_summary": "Start service at {service_address} on {effective_date}",
        "required_fields": ["name", "phone_number", "address", "account_number"],
        "menu_path": ["1", "1"],
    }
    d.update(over)
    return d


def ivr_map(**over: Any) -> dict[str, Any]:
    d = {"company_name": "Lakeshore Energy", "intents": [ivr_intent()], "confirmation_pattern": r"^LE-\d{5}$",
         "keypad_fields": ["account_number"]}
    d.update(over)
    return d


def precall_question(**over: Any) -> dict[str, Any]:
    d = {"question_id": "deposit_or_autopay", "question": "Deposit or autopay?",
         "options": [option("deposit"), option("autopay")], "default": "deposit", "stakes": 3,
         "tags": ["deposit", "autopay"]}
    d.update(over)
    return d


def trusted_sources(**over: Any) -> dict[str, Any]:
    d = {
        "min_agreeing_sources": 2,
        "companies": {
            "lakeshore-energy": {
                "name": "Lakeshore Energy",
                "official_domains": ["lakeshore-energy.example"],
                "sources": [{"url": "lakeshore-energy.example/contact.html", "title": "Contact"},
                            {"url": "directory.example/lakeshore", "title": "Directory"}],
            }
        },
    }
    d.update(over)
    return d


def compiled_call(**over: Any) -> dict[str, Any]:
    d = {
        "sonic_prompt": "You are an AI assistant calling for Alex Example.",
        "jev_templates": {}, "policy": {}, "ivr_map": ivr_map(), "intents": [], "lexicons": {},
        "stall_phrases": ["One moment please."], "say_templates": {}, "allowed_fields": {"name": "Alex Example"},
        "task_card": task_card(), "skill_versions": [],
    }
    d.update(over)
    return d


# --- A-G-4 frozen_models / A-G-5 intent_required_fields ------------------------


def test_r3_a_g_4_reference_files_validate() -> None:
    parsed = m.IvrMap.model_validate(ivr_map())
    assert parsed.intents[0].required_fields == ["name", "phone_number", "address", "account_number"]
    m.IvrMap.model_validate(ivr_map(version=1))
    m.PrecallQuestionFile.model_validate({"questions": [precall_question()]})
    m.TrustedSources.model_validate(trusted_sources())


@pytest.mark.parametrize(
    "over",
    [
        {"intents": []},
        {"intents": [ivr_intent(), ivr_intent()]},
        {"confirmation_pattern": "^LE-(\\d{5}$"},
        {"keypad_fields": ["ssn"]},
        {"menu": {"1": "x"}},
        {"intents": [ivr_intent(goal="Start service for {customer}")]},
        {"intents": [ivr_intent(task_summary="On {effective_date.year} {unknown_field}")]},
        {"intents": [ivr_intent(required_fields=["date_of_birth"])]},
        {"intents": [ivr_intent(required_fields=["name", "name"])]},
        {"intents": [ivr_intent(menu_path=["1a"])]},
    ],
    ids=["no-intents", "dup-intent", "bad-regex", "keypad-forbidden", "unknown-key", "goal-placeholder",
         "summary-placeholder", "required-forbidden", "required-dup", "menu-path-not-keys"],
)
def test_r3_a_g_4_ivr_map_rejects_bad_files(over: dict[str, Any]) -> None:
    with pytest.raises(ValidationError):
        m.IvrMap.model_validate(ivr_map(**over))


def test_r3_a_g_4_templates_only_use_extracted_request_fields() -> None:
    every = "".join("{" + f + "}" for f in m.ExtractedRequest.model_fields)
    m.IvrIntent.model_validate(ivr_intent(goal=every, task_summary="No placeholders at all"))


@pytest.mark.parametrize(
    "question",
    [
        precall_question(default="cash"),
        precall_question(options=[option("deposit")]),
        precall_question(options=[option("a"), option("a")]),
        precall_question(stakes=4),
    ],
    ids=["default-not-option", "one-option", "dup-option", "stakes-over-3"],
)
def test_r3_a_g_4_precall_question_rules(question: dict[str, Any]) -> None:
    with pytest.raises(ValidationError):
        m.PrecallQuestionFile.model_validate({"questions": [question]})


def test_r3_a_g_4_precall_question_ids_unique_and_default_may_be_null() -> None:
    m.PrecallQuestionFile.model_validate({"questions": [precall_question(default=None)]})
    with pytest.raises(ValidationError):
        m.PrecallQuestionFile.model_validate({"questions": [precall_question(), precall_question()]})


@pytest.mark.parametrize(
    "over",
    [
        {"min_agreeing_sources": 0},
        {"companies": {"lakeshore-energy": {"name": "L", "official_domains": [], "sources": [{"url": "a", "title": "b"}]}}},
        {"companies": {"lakeshore-energy": {"name": "L", "official_domains": ["a"], "sources": []}}},
        {"extra": 1},
    ],
    ids=["min-zero", "no-domains", "no-sources", "unknown-key"],
)
def test_r3_a_g_4_trusted_sources_rules(over: dict[str, Any]) -> None:
    with pytest.raises(ValidationError):
        m.TrustedSources.model_validate(trusted_sources(**over))


def test_r3_a_g_4_skill_metadata_keys() -> None:
    assert (m.SKILL_KIND_KEY, m.SKILL_COMPANY_ID_KEY) == ("waitless-kind", "company-id")
    assert m.SKILL_KINDS == ("client", "harness", "company")


# --- E-G-4 compiled_field / G-G-03 ---------------------------------------------


def test_r3_e_g_4_compiled_call_menu_parsing_fields_default_empty() -> None:
    cc = m.CompiledCall.model_validate(compiled_call())
    assert cc.press_patterns == [] and cc.digit_words == {}
    cc = m.CompiledCall.model_validate(compiled_call(press_patterns=[r"press (\w+) for (.+)"], digit_words={"nine": "9", "pound": "#"}))
    assert cc.digit_words["pound"] == "#"


@pytest.mark.parametrize(
    "over",
    [{"press_patterns": ["press (\\w+"]}, {"digit_words": {"nine": "99"}}, {"digit_words": {"nine": "x"}}, {"digit_words": {"": "1"}}],
    ids=["bad-regex", "two-keys", "not-a-key", "empty-word"],
)
def test_r3_e_g_4_compiled_call_rejects_bad_menu_fields(over: dict[str, Any]) -> None:
    with pytest.raises(ValidationError):
        m.CompiledCall.model_validate(compiled_call(**over))


# --- X-G3 header_request_text / D14-1 ------------------------------------------


def test_r3_x_g3_replay_header_needs_request_text() -> None:
    header = {"replay_version": 1, "recorded_at": NOW.isoformat(), "source_mode": "text-sim", "scenario_id": "baseline",
              "request_text": "Start electric service on November 1.",
              "skills": [], "models": {"jev": "j", "planner": "bedrock:planner-id"}}
    assert m.ReplayHeader.model_validate(header).request_text.startswith("Start")
    for bad in ({k: v for k, v in header.items() if k != "request_text"}, {**header, "request_text": ""}):
        with pytest.raises(ValidationError):
            m.ReplayHeader.model_validate(bad)


# --- 常量：R2-1、G-G-05、B-G-3/B-G-4 ------------------------------------------------


def test_r3_constants() -> None:
    assert m.USE_PREFERENCE_PREFIX == "use_preference:"
    assert m.CARRIER_ERROR_CODES == ("unknown_number", "unknown_call", "unknown_line", "unknown_scenario")
    assert "pending.answer" in m.SAY_TEMPLATE_KEYS  # G-G-05 用它作答


def test_r3_g_g_05_preference_result_shape_is_a_valid_round_result() -> None:
    res = m.DecisionResult.model_validate(decision_result(
        intent=m.USE_PREFERENCE_PREFIX + "callback_time", action="say", digits=None,
        say_text="The account holder chose evenings.", reason_code="preference"))
    assert res.intent.startswith(m.USE_PREFERENCE_PREFIX)


# --- D14-1、X-G5、X-G6：A 线 config 的可选变量 -------------------------------------


def test_r3_settings_optional_planner_replay_file_and_stats() -> None:
    from waitless import config as c

    for name, field in (("WAITLESS_PLANNER", "planner"), ("WAITLESS_CODEX_COMMAND", "codex_command"),
                        ("WAITLESS_CODEX_TIMEOUT_S", "codex_timeout_s"), ("WAITLESS_REPLAY_FILE", "replay_file"),
                        ("WAITLESS_COMPANY_STATS", "company_stats_path")):
        assert c.SETTINGS_ENV[name] == field
    base = dict(mode="text-sim", host="127.0.0.1", port=0, allowed_hosts=("127.0.0.1",), db_path="w.db", web_dir="web",
                skills_dir="skills", company_dir="company", engine_config_path="server/config/engine.yaml",
                engine=yaml.safe_load(ENGINE_YAML))
    s = c.Settings(**base)
    assert s.planner == "bedrock" and s.codex_command is None and s.replay_file is None and s.company_stats_path is None
    assert c.Settings(**base, planner="codex", codex_command=("prog", "--flag")).planner == "codex"
    for bad in ({"planner": "lite"}, {"codex_timeout_s": 0}):
        with pytest.raises(ValidationError):
            c.Settings(**base, **bad)


# ---------------------------------------------------------------------------
# r4：D 线缺口定稿（RUN/step3/gap-decisions-r4.md）
# ---------------------------------------------------------------------------


def test_r4_d_g_2_turn_seq_only_on_call_round_results() -> None:
    res = m.DecisionResult.model_validate(decision_result(intent="open_answer", action="release", digits=None, turn_seq=3))
    assert res.turn_seq == 3
    assert m.DecisionResult.model_validate(decision_result()).turn_seq is None  # 可选
    with pytest.raises(ValidationError):
        m.DecisionResult.model_validate(decision_result(turn_seq=-1))
    for point in ("said_as_decided", *m.WAIT_ONLY_POINTS):
        intent = "said_ok" if point == "said_as_decided" else "x"
        ok = decision_result(point=point, intent=intent, action="wait", digits=None)
        m.DecisionResult.model_validate(ok)
        with pytest.raises(ValidationError):
            m.DecisionResult.model_validate({**ok, "turn_seq": 0})


def test_r4_d_g_2_turn_seq_round_trips() -> None:
    obj = m.DecisionResult.model_validate(decision_result(turn_seq=7))
    assert m.DecisionResult.model_validate_json(obj.model_dump_json()).turn_seq == 7


def test_r4_d_g_11_call_phase_mapping() -> None:
    assert m.CALL_PHASE_FROM_JEV == {
        "ivr_menu": "ivr", "hold": "hold", "ai_agent": "ai_agent", "live_rep": "live_rep",
        "identity_check": "live_rep", "ended": "wrapping_up",
    }
    assert set(m.CALL_PHASE_FROM_JEV) == set(get_args(m.JevCallPhase))
    phases = set(get_args(m.CallPhase))
    assert set(m.CALL_PHASE_FROM_JEV.values()) <= phases
    assert m.ACTION_DRIVEN_PHASES == ("awaiting_user", "handoff_ready", "in_handoff", "resuming")
    assert set(m.ACTION_DRIVEN_PHASES) <= phases
    assert not set(m.ACTION_DRIVEN_PHASES) & set(m.CALL_PHASE_FROM_JEV.values())


def test_r4_d_g_4b_handoff_ended_carries_dropped_count() -> None:
    # 转接窗口里 OutputGuard 丢弃 Sonic 输出不写 trace，次数记在 handoff_ended 的 action 里（D-G-4b）
    assert m.TRACE_ACTION_KEYS["handoff_ended"] == ("dropped_sonic_outputs",)
    ev = m.TraceEvent.model_validate({"task_id": "t-1", "call_id": "c-1", "event_type": "handoff_ended",
                                      "action": {"dropped_sonic_outputs": 2}})
    assert ev.action == {"dropped_sonic_outputs": 2}
    assert "guard_blocked" not in m.CONSEQUENTIAL_EVENTS
