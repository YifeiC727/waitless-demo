"""SQLite 存储与敏感字段守卫（A 线；tech §6.8；AC-32、AC-33、AC-56、AC-58）。

公开接口（§7）：Store(path)、Store.save_profile()、ForbiddenField、SCHEMA（七张表）。
跨线用到的方法见 interfaces.Store；其余方法是 A 线内部接口，可增删。没有 BLOB 列。
"""

from collections.abc import Mapping
from pathlib import Path
from typing import Any

from waitless.interfaces import ForbiddenField
from waitless.models import Preference, Profile, StandingRule, TraceEvent

__all__ = ["SCHEMA", "SEED_STANDING_RULES", "ForbiddenField", "Store"]

# todo(A): 七张表的 DDL，字段按 tech §6.8：
#   profile(id=1, name, phone_number, email, address, zip, accounts_json, updated_at) —— 没有 ssn、card_number、date_of_birth 列；
#     accounts_json = {company_id: account_number}（A-04）
#   standing_rules(id, rule_key, rule_text, scope, active)
#   preferences(id, company_id, question_key, choice, stakes, source_call_id, created_at)
#   companies(company_id, name, official_number, line_number, sources_json, verified_at)
#   tasks(task_id, company_id, goal, request_text, task_card_json, card_version, status, created_at, updated_at)
#   calls(call_id, task_id, scenario_id, mode, started_at, ended_at, duration_s, outcome, confirmation_number,
#         skill_commits_json, interrupts_used)
#   turns(id, task_id, call_id 可空, seq, t_ms, event_type, speaker, transcript, decision_id, decision_point,
#         jev_request_json, jev_response_json, policy_decision_json, action_json, latency_ms, skill_versions_json)
#   不许出现 BLOB 类型或音频列（AC-33、AC-101）。
SCHEMA: str = ""

# todo(A): 初始化写入 3 条常设规则（R47、AC-56）：拒绝调查和推销、账单选无纸化、语言选英语。
SEED_STANDING_RULES: tuple[StandingRule, ...] = ()


class Store:
    """SQLite 存储（实现 interfaces.Store）。"""

    def __init__(self, path: str | Path) -> None:
        # todo(A): 打开 sqlite3 连接（path 来自 Settings.db_path）；check_same_thread 与线程使用方式自定。
        raise NotImplementedError

    def init(self) -> None:
        """建表（幂等）并写入初始常设规则。"""
        # todo(A): executescript(SCHEMA)；standing_rules 为空时写 SEED_STANDING_RULES。
        raise NotImplementedError

    def close(self) -> None:
        raise NotImplementedError

    # -- 跨线接口（interfaces.Store） ----------------------------------------
    def save_profile(self, fields: Mapping[str, str], *, company_id: str | None = None) -> Profile:
        """合并写入可代说字段。键在 FORBIDDEN_FIELD_KEYS 里时抛 ForbiddenField，什么都不写（AC-32）。"""
        # todo(A): 先整体检查键，再写；未知但非 forbidden 的键也抛 ForbiddenField（歧义表 A-05）。
        # todo(A): account_number → accounts_json[company_id]（A-04，每家公司一个）；没给 company_id 抛 ValueError。
        raise NotImplementedError

    def get_profile(self) -> Profile:
        raise NotImplementedError

    def list_standing_rules(self) -> list[StandingRule]:
        raise NotImplementedError

    def list_preferences(self, company_id: str) -> list[Preference]:
        raise NotImplementedError

    def add_preference(self, pref: Preference) -> None:
        """只写 stakes ≤1（AC-58）；Preference 模型已限制，这里再拒绝一次。"""
        raise NotImplementedError

    # -- A 线内部 ------------------------------------------------------------
    def upsert_company(self, company_id: str, *, name: str, official_number: str | None, line_number: str,
                       sources: list[dict[str, Any]], verified_at: str | None) -> None:
        raise NotImplementedError

    def create_task(self, *, task_id: str, company_id: str | None, goal: str, request_text: str,
                    task_card: dict[str, Any] | None, card_version: int, status: str) -> None:
        raise NotImplementedError

    def get_task(self, task_id: str) -> dict[str, Any] | None:
        """缺口 A-G-12（定稿 column_named_dicts）：本类返回行的方法（get_task、list_calls 等）都返回 dict，
        键与 §6.8 的列名完全相同，*_json 列保持 JSON 字符串，由调用方解析。"""
        raise NotImplementedError

    def update_task(self, task_id: str, **fields: Any) -> None:
        raise NotImplementedError

    def create_call(self, *, call_id: str, task_id: str, scenario_id: str | None, mode: str, started_at: str,
                    skill_commits: list[dict[str, Any]]) -> None:
        raise NotImplementedError

    def update_call(self, call_id: str, **fields: Any) -> None:
        raise NotImplementedError

    def list_calls(self, task_id: str) -> list[dict[str, Any]]:
        """一个任务可以有多通（R50、AC-25）。"""
        raise NotImplementedError

    def insert_turn(self, ev: TraceEvent) -> int:
        """写一行 turns，返回 seq（每通电话内递增；助手端与规划事件 call_id 为空）。
        不做 Trace.event() 的写入检查（A-25）：AC-34 的测试用它直接写坏行。"""
        raise NotImplementedError

    def list_turns(self, *, call_id: str | None = None, task_id: str | None = None) -> list[TraceEvent]:
        raise NotImplementedError
