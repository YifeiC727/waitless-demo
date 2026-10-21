"""Jev 客户端与 state 组装（G 线；tech §6.6、事实 13；AC-42、AC-47、AC-59、AC-88）。

公开接口（§7）：HttpJevClient.ask(state, questions)、build_state(...)。
歧义表 A-06：ask 不带决策点；歧义表 A-01：state 只由 gate 调 build_state 组装。
请求格式与 tools/jev/ask.mjs 同一契约：{"model", "state", "questions"}；返回 answers.<qid>。
URL、模型名、密钥都来自 Settings（WAITLESS_JEV_URL / _MODEL / _API_KEY），这里不写任何字面量。
"""

from collections.abc import Mapping, Sequence
from typing import Any
import httpx

from pydantic import SecretStr

from waitless.models import (
    JevQuestion,
    JevRequest,
    JevResponse,
    JevState,
    PriorChoice,
    StandingRule,
    TranscriptLine,
    JevTranscript,
    JEV_TRANSCRIPT_MAX_LINES,
    JEV_TRANSCRIPT_NOTE,
    FORBIDDEN_FIELD_KEYS,
)

__all__ = ["HttpJevClient", "JevError", "build_request", "build_state"]


class JevError(RuntimeError):
    """Jev 返回非 2xx、超时或响应不合 JevResponse。不悄悄退到回放或默认答案（C14）。"""


class JevInvalidResponse(JevError):
    """Invalid wire payload, retained for the per-request decision snapshot."""

    def __init__(self, raw):
        super().__init__('Jev returned an invalid response')
        self.response = raw if isinstance(raw, dict) else None


class HttpJevClient:
    """实现 interfaces.JevClient。"""

    def __init__(
        self,
        *,
        url: str,
        model: str,
        api_key: SecretStr,
        timeout_s: float,
        http: Any | None = None,
    ) -> None:
        """http：可注入的 httpx.AsyncClient（测试用 MockTransport）。timeout_s 按 S0-r2 V10 的 p95 数倍设定。"""
        if not url or not model or not api_key.get_secret_value() or timeout_s <= 0:
            raise JevError("Jev URL, model, API key and positive timeout are required")
        self._url, self._model, self._api_key = url, model, api_key
        self._timeout_s = timeout_s
        self._http = http if http is not None else httpx.AsyncClient(timeout=timeout_s)

    async def ask(self, state: JevState, questions: Mapping[str, JevQuestion]) -> JevResponse:
        request = build_request(self._model, state, questions)
        raw = None
        try:
            response = await self._http.post(
                self._url, json=request.model_dump(mode="json", exclude_none=True),
                headers={"Authorization": "Bearer " + self._api_key.get_secret_value()},
                timeout=self._timeout_s)
            response.raise_for_status()
            raw = response.json()
            # Check JSON primitives against the REQUEST, before model coercion.
            if (not isinstance(raw, dict) or not isinstance(raw.get("answers"), dict)
                    or set(raw["answers"]) != set(questions)):
                raise ValueError("answer keys differ from question keys")
            for qid, question in questions.items():
                answer = raw["answers"][qid]
                if question.type == "choice":
                    if (not isinstance(answer, dict) or answer.get("type") != question.type
                            or not isinstance(answer.get("choice"), str)
                            or answer["choice"] not in question.criteria
                            or type(answer.get("confidence")) not in (int, float)
                            or not isinstance(answer.get("probabilities"), dict)
                            or any(key not in question.criteria or type(value) not in (int, float)
                                   for key, value in answer["probabilities"].items())):
                        raise ValueError("invalid raw choice answer")
            result = JevResponse.model_validate(raw)
            if set(result.answers) != set(questions):
                raise ValueError("answer keys differ from question keys")
            for qid, question in questions.items():
                answer = result.answers[qid]
                if answer.type != question.type:
                    raise ValueError("answer type differs from question type")
                if question.type == "choice":
                    if (not answer.probabilities or answer.choice not in question.criteria
                            or not set(answer.probabilities) <= set(question.criteria)):
                        raise ValueError("answer contains unknown criteria")
                if question.type == "score" and not 0 <= answer.score <= len(question.criteria) - 1:
                    raise ValueError("score is outside the question scale")
            return result
        except (ValueError, TypeError, AttributeError):
            raise JevInvalidResponse(raw) from None
        except Exception:
            # HTTP bodies and validation errors may echo inputs; never expose them.
            raise JevError("Jev request failed or returned an invalid response") from None

    async def aclose(self) -> None:
        await self._http.aclose()


def build_state(
    *,
    task_goal: str,
    allowed_fields: Mapping[str, str],
    standing_rules: Sequence[StandingRule],
    prior_choices: Sequence[PriorChoice],
    transcript: Sequence[TranscriptLine],
) -> JevState:
    """§6.6：user_profile 只放可代说字段（AC-42）；transcript 取最近 8 行、最后一行是本轮，
    写成 {"note": JEV_TRANSCRIPT_NOTE, "lines": [...]}；转写只进这里，不进 instructions（AC-59、H4）。
    standing_rules 写成规则文字列表（A-26）；task_goal 由 gate 传入（有卡取 task_card.goal，否则
    registry.default_task_goal）。调用方交来的 transcript 不预先截断，截断只在这里（A-01）。"""
    return JevState(task_goal=task_goal,
                    user_profile={k: v for k, v in allowed_fields.items() if k not in FORBIDDEN_FIELD_KEYS},
                    standing_rules=[r.rule_text for r in standing_rules], prior_choices=list(prior_choices),
                    transcript=JevTranscript(note=JEV_TRANSCRIPT_NOTE,
                                             lines=list(transcript)[-JEV_TRANSCRIPT_MAX_LINES:]))


def build_request(model: str, state: JevState, questions: Mapping[str, JevQuestion]) -> JevRequest:
    return JevRequest(model=model, state=state, questions=dict(questions))
