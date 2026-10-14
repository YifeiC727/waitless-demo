"""挂断后的结果流程（A 线；tech §6.6 DP-E1、DP-E2、§6.7 L4、L5、§8 第 12 步；AC-25、AC-37、AC-38）。

公开接口（§7）：finalize(call_id) -> CaseResult、case_result(task_id)。
歧义表 A-24（定稿 methods_callresult）：§7 的 CaseResult 落成 models.CallResult（一通电话的结论），
case_result(task_id) 再把该任务全部 CallResult 合并成 models.CaseResultOut。
"""

import asyncio
import json
import re
import uuid
import yaml
from datetime import datetime, timedelta
from waitless.interfaces import ToolFailure
from waitless.models import (CallTranscriptLine, DecisionContext, DecisionRequest, TranscriptLine, TaskCard,
    IvrMap, SKILL_COMPANY_ID_KEY, VerifiedItem, FollowUpItem, Evidence, CallSummary, HistoryItem)
from waitless.tasks import _confidence

from collections.abc import Mapping

from waitless.interfaces import Clock, DecisionGate, LlmTasks, Store, Trace
from waitless.models import CallResult, CaseResultOut, Skill

__all__ = ["ResultService"]


_NUMBER_UNITS = {word: value for value, word in enumerate(
    "zero one two three four five six seven eight nine ten eleven twelve thirteen fourteen fifteen sixteen seventeen eighteen nineteen".split())}
_NUMBER_UNITS["oh"] = 0
_NUMBER_TENS = {word: 10 * value for value, word in enumerate(
    "twenty thirty forty fifty sixty seventy eighty ninety".split(), start=2)}
_NUMBER_SCALES = {"hundred": 100, "thousand": 1000}
_NUMBER_WORDS = _NUMBER_UNITS.keys() | _NUMBER_TENS.keys() | _NUMBER_SCALES.keys()


def _number_word_digits(words: list[str]) -> str:
    """Evaluate cardinal groups; concatenate successive spoken digits or groups."""
    groups, current, total, last = [], None, 0, None

    def flush():
        nonlocal current, total
        if current is not None or total:
            groups.append(str(total + (current if current is not None else 0)))
        current, total = None, 0

    for word in words:
        if word in _NUMBER_UNITS:
            value = _NUMBER_UNITS[word]
            if current is not None and (last in _NUMBER_UNITS or last == "unit_after_tens"
                                        or (last in _NUMBER_TENS and value >= 10)):
                flush()
            if current is not None and last in _NUMBER_TENS and 0 < value < 10:
                current += value
                last = "unit_after_tens"
                continue
            # Zero/oh after a tens group is a separate spoken digit.
            if current is not None and last in _NUMBER_TENS and value == 0:
                flush()
            current = (current if current is not None else 0) + value
        elif word in _NUMBER_TENS:
            if current is not None and last not in _NUMBER_SCALES:
                flush()
            current = (current if current is not None else 0) + _NUMBER_TENS[word]
        else:
            # A scale cannot supply an unspoken leading one.
            if current is None:
                return ""
            if word == "hundred":
                current *= 100
            else:
                total += current * 1000
                current = None
        last = word
    flush()
    return "".join(groups)


def _pattern_separators(pattern: str) -> list[str]:
    """Use literal punctuation only, excluding regex syntax and range operators."""
    pattern = re.sub(r"\{\d+(?:,\d*)?\}", "", pattern)
    separators, in_class, index = set(), False, 0
    while index < len(pattern):
        char = pattern[index]
        if not in_class and pattern.startswith("(?", index):
            # Group names, flags and lookaround markers are regex syntax.
            prefix = re.match(r"\(\?(?:P<[^>]+>|[aiLmsux-]*:|[=!]|<[=!]|>|[aiLmsux-]+(?=\)))", pattern[index:])
            if prefix:
                index += prefix.end()
                continue
        if char == "\\" and index + 1 < len(pattern):
            index += 1
            char = pattern[index]
            if not char.isalnum():
                separators.add(char)
        elif char == "[":
            in_class = True
        elif char == "]":
            in_class = False
        elif in_class:
            is_range = (char == "-" and index > 0 and index + 1 < len(pattern)
                        and pattern[index - 1].isalnum() and pattern[index + 1].isalnum())
            if not char.isalnum() and char != "^" and not is_range:
                separators.add(char)
        elif not char.isalnum() and char not in ".^$*+?{}()|":
            separators.add(char)
        index += 1
    return [""] + sorted(separators)


def _confirmation_candidates(text: str, pattern: str) -> list[str]:
    """Normalize existing letters/digits, returning only complete pattern matches."""
    tokens = re.findall(r"[a-z]+|[0-9]+", text.lower())
    parts, index = [], 0
    while index < len(tokens):
        token = tokens[index]
        if token in _NUMBER_WORDS:
            end = index + 1
            while end < len(tokens) and (tokens[end] in _NUMBER_WORDS or tokens[end] == "and"):
                end += 1
            digits = _number_word_digits([word for word in tokens[index:end] if word != "and"])
            parts.append(("digits" if digits else "other", digits))
            index = end
        else:
            kind = "digits" if token.isdigit() else ("letters" if len(token) <= 3 and token != "and" else "other")
            parts.append((kind, token if kind == "digits" else token.upper()))
            index += 1
    found = []
    separators = _pattern_separators(pattern)
    for start in range(len(parts)):
        letters, end = "", start
        while end < len(parts) and parts[end][0] == "letters":
            letters += parts[end][1]
            end += 1
        digits = ""
        while end < len(parts) and parts[end][0] == "digits":
            digits += parts[end][1]
            end += 1
        for separator in separators:
            candidate = letters + separator + digits if letters and digits else letters or digits
            if candidate and re.fullmatch(pattern, candidate) and candidate not in found:
                found.append(candidate)
    return found


class ResultService:
    def __init__(
        self,
        *,
        store: Store,
        trace: Trace,
        gate: DecisionGate,
        llm: LlmTasks,
        skills: Mapping[str, Skill],
        clock: Clock,
    ) -> None:
        """缺口 A-G-3（定稿 skills_and_clock）：skills 用来按任务的 company_id 找公司 skill
        （metadata 的 company-id，models.SKILL_COMPANY_ID_KEY），读 references/ivr-map.yaml（按 models.IvrMap 校验）
        的 confirmation_pattern；clock 给 ended_at = clock.now_iso()，duration_s 按 calls.started_at 与 clock 算。"""
        self.store, self.trace, self.gate, self.llm = store, trace, gate, llm
        self.skills, self.clock = skills, clock
        self._locks = {}

    async def finalize(self, call_id: str) -> CallResult:
        """L4 抽确认号与陈述 → DP-E1 判结果 → 每条陈述问 DP-E2 → L5 写摘要 → 写 calls 行，返回 CallResult。"""
        async with self._locks.setdefault(call_id, asyncio.Lock()):
            events = self.trace.events(call_id=call_id)
            if not events:
                raise ValueError("Call has no trace events")
            task_id = events[0].task_id
            row = next((r for r in self.store.list_calls(task_id) if r["call_id"] == call_id), None)
            if row is None:
                raise ValueError("Unknown call")
            if row["result_json"]:
                return CallResult.model_validate_json(row["result_json"])
            ended_at = row["ended_at"] or self.clock.now_iso()
            ended_event = next((e for e in reversed(events) if e.event_type == "call_ended"), None)
            if not row["ended_at"] and ended_event is not None and ended_event.t_ms is not None:
                elapsed_ms = max(0, self.clock.monotonic_ms() - ended_event.t_ms)
                ended_at = (datetime.fromisoformat(ended_at) - timedelta(milliseconds=elapsed_ms)).isoformat()
            task = self.store.get_task(task_id)
            card = TaskCard.model_validate_json(task["task_card_json"]) if task["task_card_json"] else None
            transcript = [CallTranscriptLine(seq=e.seq, t_ms=e.t_ms, speaker=e.speaker, text=e.transcript)
                          for e in events if e.event_type in ("company_turn", "waitless_utterance")]
            items = await self.llm.extract_result_items(transcript)
            skill = next(s for s in self.skills.values() if s.metadata.get(SKILL_COMPANY_ID_KEY) == task["company_id"])
            ivr = IvrMap.model_validate(yaml.safe_load((skill.path / "references/ivr-map.yaml").read_text(encoding="utf-8")))
            matching = next((n for n in items.confirmation_numbers if re.fullmatch(ivr.confirmation_pattern, n)), None)
            if matching is None:
                company_numbers = {number for event in events if event.event_type == "company_turn"
                                   for number in _confirmation_candidates(event.transcript, ivr.confirmation_pattern)}
                matching = next((number for candidate in items.confirmation_numbers
                                 for number in _confirmation_candidates(candidate, ivr.confirmation_pattern)
                                 if number in company_numbers), None)
            context = DecisionContext(task_card=card, allowed_fields={}, standing_rules=self.store.list_standing_rules(),
                                      prior_choices=[], interrupts_used=0, phase="ended")
            async def decide(point, lines):
                return await self.gate.decide(point, DecisionRequest(decision_id=uuid.uuid4().hex, point=point,
                           call_id=call_id, task_id=task_id, turn=None, candidates=[], context=context, transcript=lines))
            decision = await decide("call_outcome", [TranscriptLine(speaker=l.speaker, text=l.text) for l in transcript])
            outcome = "needs_follow_up"
            if decision.intent == "confirmed" and _confidence(decision) >= 0.85 and matching:
                outcome = "confirmed"
            elif decision.intent == "failed" and _confidence(decision) >= 0.85:
                outcome = "failed"
            number = matching if outcome == "confirmed" else None
            verified, follow_ups = [], []
            company_seqs = {l.seq for l in transcript if l.speaker == "company"}
            for statement in items.statements:
                kind = await decide("result_item_kind", [TranscriptLine(speaker="company", text=statement.text)])
                if kind.intent == "verified" and _confidence(kind) >= 0.85 and statement.turn_seq in company_seqs:
                    verified.append(VerifiedItem(label="Confirmed", value=statement.text,
                                 evidence=Evidence(call_id=call_id, turn_seq=statement.turn_seq)))
                elif kind.intent != "drop" or _confidence(kind) < 0.85:
                    follow_ups.append(FollowUpItem(label="Needs follow-up", detail=statement.text))
            result = CallResult(call_id=call_id, outcome=outcome, confirmation_number=number,
                                verified=verified, follow_ups=follow_ups, summary="")
            summary_input = CaseResultOut(task_id=task_id, company_name=ivr.company_name, outcome=outcome,
                              headline=outcome.replace("_", " ").title(), verified=verified, follow_ups=follow_ups,
                              confirmation_number=number, summary="", calls=[], history=[])
            result.summary = await self.llm.write_summary(result=summary_input, transcript=transcript)
            summary_lower = result.summary.lower()
            if outcome != "confirmed" and ("confirmed" in summary_lower or "confirmation number" in summary_lower
                                           or _confirmation_candidates(result.summary, ivr.confirmation_pattern)):
                status = "needs follow-up" if outcome == "needs_follow_up" else "failed"
                result.summary = (f"The call with {ivr.company_name} {status}. "
                                  f"There are {len(follow_ups)} follow-up items.")
            duration = max(0, (datetime.fromisoformat(ended_at)-datetime.fromisoformat(row["started_at"])).total_seconds())
            interrupts = sum(e.event_type in ("pending_choice_created", "handoff_ready") for e in events)
            self.store.update_call(call_id, outcome=outcome, confirmation_number=number, ended_at=ended_at,
                                   duration_s=duration, interrupts_used=interrupts, result_json=result.model_dump_json())
            return result

    def case_result(self, task_id: str) -> CaseResultOut:
        """合并该任务全部通话：calls 每通一条，history 含各通事件（AC-25）。进行中时 outcome=in_progress，
        进行中那一通的 ended_at、duration_s 为 null（A-10）。"""
        task = self.store.get_task(task_id)
        if task is None:
            raise ToolFailure("unknown_task")
        card = TaskCard.model_validate_json(task["task_card_json"]) if task["task_card_json"] else None
        rows = self.store.list_calls(task_id)
        results = [CallResult.model_validate_json(r["result_json"]) for r in rows if r["result_json"]]
        latest = results[-1] if results else None
        outcome = "in_progress" if not rows or any(not r["ended_at"] for r in rows) else rows[-1]["outcome"]
        number = latest.confirmation_number if latest and outcome == "confirmed" else None
        history = []
        for row in rows:
            start = datetime.fromisoformat(row["started_at"])
            events = self.trace.events(call_id=row["call_id"])
            base = next((e.t_ms for e in events if e.event_type == "call_started"), events[0].t_ms if events else 0) or 0
            for ev in events:
                if ev.event_type == "call_ended" and row["ended_at"]:
                    when = datetime.fromisoformat(row["ended_at"])
                else:
                    when = start + timedelta(milliseconds=(ev.t_ms or base)-base)
                history.append(HistoryItem(t=when, title=ev.event_type.replace("_", " ").title(),
                             detail=ev.transcript or f"{row['call_id']}: {ev.event_type}"))
        return CaseResultOut(task_id=task_id, company_name=card.company.name if card else (task["company_id"] or ""),
                             outcome=outcome, headline=outcome.replace("_", " ").title(),
                             verified=[v for r in results for v in r.verified], follow_ups=[f for r in results for f in r.follow_ups],
                             confirmation_number=number, summary=latest.summary if latest else "The call is in progress.",
                             calls=[CallSummary(call_id=r["call_id"], scenario_id=r["scenario_id"], started_at=r["started_at"],
                                      ended_at=r["ended_at"], duration_s=r["duration_s"], outcome=r["outcome"], interrupts_used=r["interrupts_used"])
                                    for r in rows], history=history)
