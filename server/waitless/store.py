"""SQLite 存储与敏感字段守卫（A 线；tech §6.8；AC-32、AC-33、AC-56、AC-58）。

公开接口（§7）：Store(path)、Store.save_profile()、ForbiddenField、SCHEMA（七张表）。
跨线用到的方法见 interfaces.Store；其余方法是 A 线内部接口，可增删。没有 BLOB 列。
"""

import json
import sqlite3
from datetime import datetime, timezone

from collections.abc import Mapping
from pathlib import Path
from typing import Any

from waitless.interfaces import ForbiddenField
from waitless.models import Preference, Profile, StandingRule, TraceEvent

__all__ = ["SCHEMA", "SEED_STANDING_RULES", "ForbiddenField", "Store"]

SCHEMA: str = """
CREATE TABLE IF NOT EXISTS profile (id INTEGER PRIMARY KEY CHECK(id=1), name TEXT, phone_number TEXT, email TEXT, address TEXT, zip TEXT, accounts_json TEXT, updated_at TEXT);
CREATE TABLE IF NOT EXISTS standing_rules (id INTEGER PRIMARY KEY, rule_key TEXT UNIQUE, rule_text TEXT, scope TEXT, active INTEGER);
CREATE TABLE IF NOT EXISTS preferences (id INTEGER PRIMARY KEY, company_id TEXT, question_key TEXT, choice TEXT, stakes INTEGER, source_call_id TEXT, created_at TEXT);
CREATE TABLE IF NOT EXISTS companies (company_id TEXT PRIMARY KEY, name TEXT, official_number TEXT, line_number TEXT, sources_json TEXT, verified_at TEXT);
CREATE TABLE IF NOT EXISTS tasks (task_id TEXT PRIMARY KEY, company_id TEXT, goal TEXT, request_text TEXT, task_card_json TEXT, card_version INTEGER, status TEXT, created_at TEXT, updated_at TEXT, intent_id TEXT);
CREATE TABLE IF NOT EXISTS calls (call_id TEXT PRIMARY KEY, task_id TEXT, scenario_id TEXT, mode TEXT, started_at TEXT, ended_at TEXT, duration_s INTEGER, outcome TEXT, confirmation_number TEXT, skill_commits_json TEXT, interrupts_used INTEGER, result_json TEXT);
CREATE TABLE IF NOT EXISTS turns (id INTEGER PRIMARY KEY, task_id TEXT, call_id TEXT, seq INTEGER, t_ms INTEGER, event_type TEXT, speaker TEXT, transcript TEXT, decision_id TEXT, decision_point TEXT, jev_request_json TEXT, jev_response_json TEXT, policy_decision_json TEXT, action_json TEXT, latency_ms INTEGER, skill_versions_json TEXT);
"""

SEED_STANDING_RULES: tuple[StandingRule, ...] = (
    StandingRule(rule_key="decline_offers", rule_text="Decline surveys and sales offers.", scope="all"),
    StandingRule(rule_key="paperless", rule_text="Choose paperless billing.", scope="all"),
    StandingRule(rule_key="english", rule_text="Choose English.", scope="all"),
)


class Store:
    """SQLite 存储（实现 interfaces.Store）。"""

    def __init__(self, path: str | Path) -> None:
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._con = sqlite3.connect(str(path), check_same_thread=False)
        self._con.row_factory = sqlite3.Row
        self.init()

    def init(self) -> None:
        """建表（幂等）并写入初始常设规则。"""
        with self._con:
            self._con.executescript(SCHEMA)
            for table, column in (("tasks", "intent_id"), ("calls", "result_json")):
                existing = {r[1] for r in self._con.execute(f"PRAGMA table_info({table})")}
                if column not in existing:
                    self._con.execute(f"ALTER TABLE {table} ADD COLUMN {column} TEXT")
            self._con.execute("INSERT OR IGNORE INTO profile (id, accounts_json) VALUES (1, '{}')")
            for rule in SEED_STANDING_RULES:
                self._con.execute("INSERT OR IGNORE INTO standing_rules (rule_key, rule_text, scope, active) VALUES (?, ?, ?, 1)",
                                  (rule.rule_key, rule.rule_text, rule.scope))

    def close(self) -> None:
        self._con.close()

    # -- 跨线接口（interfaces.Store） ----------------------------------------
    def save_profile(self, fields: Mapping[str, str], *, company_id: str | None = None) -> Profile:
        """合并写入可代说字段。键在 FORBIDDEN_FIELD_KEYS 里时抛 ForbiddenField，什么都不写（AC-32）。"""
        allowed = {"name", "phone_number", "email", "address", "zip", "account_number"}
        bad = [key for key in fields if key not in allowed]
        if bad:
            raise ForbiddenField(bad)
        if "account_number" in fields and not company_id:
            raise ValueError("account_number requires company_id")
        profile = self.get_profile().model_dump()
        for key, value in fields.items():
            if key == "account_number":
                profile["accounts"][company_id] = value
            else:
                profile[key] = value
        profile = Profile.model_validate(profile)
        data = profile.model_dump()
        data["accounts_json"] = json.dumps(data.pop("accounts"))
        data["updated_at"] = datetime.now(timezone.utc).isoformat()
        self._update("profile", "id", 1, data)
        return profile

    def get_profile(self) -> Profile:
        row = dict(self._con.execute("SELECT * FROM profile WHERE id=1").fetchone())
        return Profile(**{k: row[k] for k in ("name", "phone_number", "email", "address", "zip")},
                       accounts=json.loads(row["accounts_json"]))

    def list_standing_rules(self) -> list[StandingRule]:
        return [StandingRule(rule_key=r["rule_key"], rule_text=r["rule_text"], scope=r["scope"])
                for r in self._con.execute("SELECT * FROM standing_rules WHERE active=1 ORDER BY id")]

    def list_preferences(self, company_id: str) -> list[Preference]:
        return [Preference.model_validate({k: r[k] for k in Preference.model_fields})
                for r in self._con.execute("SELECT * FROM preferences WHERE company_id=? ORDER BY id", (company_id,))]

    def add_preference(self, pref: Preference) -> None:
        """只写 stakes ≤1（AC-58）；Preference 模型已限制，这里再拒绝一次。"""
        pref = Preference.model_validate(pref.model_dump())
        self._insert("preferences", pref.model_dump(mode="json"))

    # -- A 线内部 ------------------------------------------------------------
    def upsert_company(self, company_id: str, *, name: str, official_number: str | None, line_number: str,
                       sources: list[dict[str, Any]], verified_at: str | None) -> None:
        data = dict(company_id=company_id, name=name, official_number=official_number, line_number=line_number,
                    sources_json=json.dumps(sources), verified_at=verified_at)
        with self._con:
            self._con.execute("INSERT INTO companies (company_id, name, official_number, line_number, sources_json, verified_at) "
                              "VALUES (:company_id, :name, :official_number, :line_number, :sources_json, :verified_at) "
                              "ON CONFLICT(company_id) DO UPDATE SET name=excluded.name, official_number=excluded.official_number, "
                              "line_number=excluded.line_number, sources_json=excluded.sources_json, verified_at=excluded.verified_at", data)

    def create_task(self, *, task_id: str, company_id: str | None, goal: str, request_text: str,
                    task_card: dict[str, Any] | None, card_version: int, status: str) -> None:
        now = datetime.now(timezone.utc).isoformat()
        self._insert("tasks", dict(task_id=task_id, company_id=company_id, goal=goal, request_text=request_text,
                     task_card_json=json.dumps(task_card) if task_card is not None else None, card_version=card_version,
                     status=status, created_at=now, updated_at=now))

    def get_task(self, task_id: str) -> dict[str, Any] | None:
        """缺口 A-G-12（定稿 column_named_dicts）：本类返回行的方法（get_task、list_calls 等）都返回 dict，
        键与 §6.8 的列名完全相同，*_json 列保持 JSON 字符串，由调用方解析。"""
        row = self._con.execute("SELECT * FROM tasks WHERE task_id=?", (task_id,)).fetchone()
        return dict(row) if row else None

    def get_company(self, company_id: str) -> dict[str, Any] | None:
        row = self._con.execute("SELECT * FROM companies WHERE company_id=?", (company_id,)).fetchone()
        return dict(row) if row else None

    def update_task(self, task_id: str, **fields: Any) -> None:
        if "task_card" in fields:
            fields["task_card_json"] = json.dumps(fields.pop("task_card"))
        fields.setdefault("updated_at", datetime.now(timezone.utc).isoformat())
        self._update("tasks", "task_id", task_id, fields)

    def create_call(self, *, call_id: str, task_id: str, scenario_id: str | None, mode: str, started_at: str,
                    skill_commits: list[dict[str, Any]]) -> None:
        self._insert("calls", dict(call_id=call_id, task_id=task_id, scenario_id=scenario_id, mode=mode,
                     started_at=started_at, skill_commits_json=json.dumps(skill_commits), outcome="in_progress", interrupts_used=0))

    def update_call(self, call_id: str, **fields: Any) -> None:
        self._update("calls", "call_id", call_id, fields)

    def list_calls(self, task_id: str) -> list[dict[str, Any]]:
        """一个任务可以有多通（R50、AC-25）。"""
        return [dict(row) for row in self._con.execute("SELECT * FROM calls WHERE task_id=? ORDER BY rowid", (task_id,))]

    def insert_turn(self, ev: TraceEvent) -> int:
        """写一行 turns，返回 seq（每通电话内递增；助手端与规划事件 call_id 为空）。
        不做 Trace.event() 的写入检查（A-25）：AC-34 的测试用它直接写坏行。"""
        data = ev.model_dump(mode="json")
        with self._con:
            seq = self._con.execute("SELECT COALESCE(MAX(seq), 0)+1 FROM turns WHERE call_id IS ? AND task_id=?",
                                    (ev.call_id, ev.task_id)).fetchone()[0]
            data["seq"] = seq
            for key in ("jev_request", "jev_response", "policy_decision", "action", "skill_versions"):
                value = data.pop(key)
                data[key+"_json"] = json.dumps(value) if value is not None else None
            self._insert("turns", data)
        return seq

    def list_turns(self, *, call_id: str | None = None, task_id: str | None = None) -> list[TraceEvent]:
        clauses, args = [], []
        if call_id is not None:
            clauses.append("call_id=?")
            args.append(call_id)
        if task_id is not None:
            clauses.append("task_id=?")
            args.append(task_id)
        query = "SELECT * FROM turns" + (" WHERE " + " AND ".join(clauses) if clauses else "") + " ORDER BY id"
        events = []
        for row in self._con.execute(query, args):
            data = dict(row)
            data.pop("id")
            for key in ("jev_request", "jev_response", "policy_decision", "action", "skill_versions"):
                value = data.pop(key+"_json")
                data[key] = json.loads(value) if value is not None else None
            events.append(TraceEvent.model_validate(data))
        return events

    def _insert(self, table: str, fields: dict[str, Any]) -> None:
        columns = {r[1] for r in self._con.execute(f"PRAGMA table_info({table})")}
        if not fields.keys() <= columns:
            raise ValueError("unknown column")
        with self._con:
            self._con.execute(f"INSERT INTO {table} ({', '.join(fields)}) VALUES ({', '.join('?' for _ in fields)})", tuple(fields.values()))

    def _update(self, table: str, key: str, value: Any, fields: dict[str, Any]) -> None:
        columns = {r[1] for r in self._con.execute(f"PRAGMA table_info({table})")}
        if not fields.keys() <= columns:
            raise ValueError("unknown column")
        if fields:
            with self._con:
                self._con.execute(f"UPDATE {table} SET {', '.join(k+'=?' for k in fields)} WHERE {key}=?", (*fields.values(), value))
