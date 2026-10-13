"""公司方的 AI 客服与坐席（B 线；tech §6.10 末段；K14）。属于对方，不受 D10 约束，可以有公司侧工具。

公开接口（§7）：CompanyBot。live-sim 才用；text-sim 用 company/text.py。公司进程不把转写写到磁盘。
"""

from typing import Any

from waitless.company.engine import ScenarioEngine
from waitless.config import Settings
from waitless.models import CompanyBotRole

__all__ = ["COMPANY_TOOLS", "CompanyBot"]

# 公司侧工具名（用于推进场景状态）
COMPANY_TOOLS: tuple[str, ...] = (
    "record_detail",
    "verify_account",
    "transfer_to_representative",
    "request_account_holder",
    "account_holder_verified",
    "present_offer",
    "record_answer",
    "issue_confirmation",
    "end_call",
)


class CompanyBot:
    """agent 用 voices.agent_sonic，rep 用 voices.rep_sonic；persona 取场景步骤的 persona。"""

    def __init__(self, *, role: CompanyBotRole, engine: ScenarioEngine, settings: Settings) -> None:
        raise NotImplementedError

    async def run(self, websocket: Any) -> None:
        """经 /ws/company-bot 收发 Twilio 格式媒体；跑公司方的 Pipecat + Nova Sonic 管线（带 COMPANY_TOOLS）。"""
        raise NotImplementedError
