"""Jev 客户端与 state 组装（G 线；tech §6.6、事实 13；AC-42、AC-47、AC-59、AC-88）。

公开接口（§7）：HttpJevClient.ask(state, questions)、build_state(...)。
歧义表 A-06：ask 不带决策点；歧义表 A-01：state 只由 gate 调 build_state 组装。
请求格式与 tools/jev/ask.mjs 同一契约：{"model", "state", "questions"}；返回 answers.<qid>。
URL、模型名、密钥都来自 Settings（WAITLESS_JEV_URL / _MODEL / _API_KEY），这里不写任何字面量。
"""

from collections.abc import Mapping, Sequence
from typing import Any

from pydantic import SecretStr

from waitless.models import (
    JevQuestion,
    JevRequest,
    JevResponse,
    JevState,
    PriorChoice,
    StandingRule,
    TranscriptLine,
)

__all__ = ["HttpJevClient", "JevError", "build_request", "build_state"]


class JevError(RuntimeError):
    """Jev 返回非 2xx、超时或响应不合 JevResponse。不悄悄退到回放或默认答案（C14）。"""


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
        raise NotImplementedError

    async def ask(self, state: JevState, questions: Mapping[str, JevQuestion]) -> JevResponse:
        # todo(G): POST JevRequest(model, state, questions).model_dump(mode="json")；Authorization 头用 api_key，
        #          不打印、不写日志。返回值按 JevResponse 校验，answers 的键必须与 questions 一致。
        raise NotImplementedError

    async def aclose(self) -> None:
        raise NotImplementedError


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
    raise NotImplementedError


def build_request(model: str, state: JevState, questions: Mapping[str, JevQuestion]) -> JevRequest:
    raise NotImplementedError
