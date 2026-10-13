"""回放文件与请求样例（第 3 步写，冻结；AC-88、AC-89）。

手写回放文件的格式照 tech §6.12：第一行 ReplayHeader，其余每行 {"channel","seq","shape","response"}；
同一通道 seq 从 0 起连续。请求样例给 ReplayPlayer.jev_client().ask() 与 converse_client().converse() 用。
"""

import json
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Any

from waitless import models as m

ROUND_QIDS: list[str] = list(m.CALL_ROUND_QUESTIONS)  # menu_choice, call_phase, needs_user, stakes


def jev_state(last_text: str = "Main menu. To start new service press 1.") -> m.JevState:
    return m.JevState(
        task_goal="Start electricity service on November 1",
        user_profile={"name": "Alex Example"},
        standing_rules=["Decline surveys and marketing offers."],
        prior_choices=[],
        transcript=m.JevTranscript(note=m.JEV_TRANSCRIPT_NOTE, lines=[m.TranscriptLine(speaker="company", text=last_text)]),
    )


def round_questions() -> dict[str, m.JevQuestion]:
    return {
        "menu_choice": m.JevQuestion(
            type="choice",
            instructions="The other side is an automated phone menu. Pick the option that best advances the task goal.",
            criteria={"1": "start new service", "none_of_the_above": "none of these"},
        ),
        "call_phase": m.JevQuestion(
            type="choice",
            instructions="Which phase is the call in right now?",
            criteria={
                "ivr_menu": "An automated menu listing options",
                "hold": "Hold music or a wait message",
                "ai_agent": "An automated virtual agent asking questions",
                "live_rep": "A human representative speaking",
                "identity_check": "Someone asking to verify the account holder's identity",
                "ended": "The call has ended",
            },
        ),
        "needs_user": m.JevQuestion(type="noul", instructions="Answering this step needs the account holder."),
        "stakes": m.JevQuestion(
            type="score",
            instructions="How serious would a wrong answer to this step be?",
            criteria=["0: no consequence", "1: minor inconvenience", "2: changes the account", "3: involves money"],
        ),
    }


def said_questions() -> dict[str, m.JevQuestion]:
    return {"said": m.JevQuestion(type="noul", instructions="Did Waitless's last utterance convey the decision?")}


def round_response(choice: str = "1") -> dict[str, Any]:
    return {
        "answers": {
            "menu_choice": {"type": "choice", "choice": choice, "confidence": 0.95, "probabilities": {choice: 0.95, "none_of_the_above": 0.05}},
            "call_phase": {"type": "choice", "choice": "ivr_menu", "confidence": 0.9, "probabilities": {"ivr_menu": 0.9, "hold": 0.1}},
            "needs_user": {"type": "noul", "noul": 0.02},
            "stakes": {"type": "score", "score": 0.0, "confidence": 0.9},
        },
        "model": "test-jev-model",
    }


def said_response(p: float = 0.96) -> dict[str, Any]:
    return {"answers": {"said": {"type": "noul", "noul": p}}, "model": "test-jev-model"}


def offer_call(tool: str | None = "extract_offer_options_tool", text: str = "November 1 for $25, or November 3 at no charge.") -> m.ConverseCall:
    tool_config = None
    if tool is not None:
        tool_config = {
            "tools": [{"toolSpec": {"name": tool, "description": "extract options", "inputSchema": {"json": {"type": "object"}}}}],
            "toolChoice": {"tool": {"name": tool}},
        }
    return m.ConverseCall(
        task="extract_offer_options",
        model_id="test-planner-model",
        messages=[{"role": "user", "content": [{"text": text}]}],
        tool_config=tool_config,
    )


def summary_call() -> m.ConverseCall:
    return m.ConverseCall(
        task="write_summary",
        model_id="test-planner-model",
        messages=[{"role": "user", "content": [{"text": "Summarize the call."}]}],
    )


def record_line(channel: str, seq: int, shape: Mapping[str, Any], response: Mapping[str, Any]) -> dict[str, Any]:
    rec = m.ReplayRecord.model_validate({"channel": channel, "seq": seq, "shape": dict(shape), "response": dict(response)})
    return rec.model_dump(mode="json", exclude_none=True)


def write_replay(path: Path, header: m.ReplayHeader, records: Sequence[Mapping[str, Any]]) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    lines = [header.model_dump_json()] + [json.dumps(r) for r in records]
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return path


def read_replay(path: Path) -> tuple[m.ReplayHeader, list[m.ReplayRecord], str]:
    text = path.read_text(encoding="utf-8")
    rows = [ln for ln in text.splitlines() if ln.strip()]
    header = m.ReplayHeader.model_validate_json(rows[0])
    records = [m.ReplayRecord.model_validate_json(r) for r in rows[1:]]
    return header, records, text


__all__ = [
    "ROUND_QIDS",
    "jev_state",
    "offer_call",
    "read_replay",
    "record_line",
    "round_questions",
    "round_response",
    "said_questions",
    "said_response",
    "summary_call",
    "write_replay",
]
