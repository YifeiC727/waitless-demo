"""AC-33：turns 表含 transcript、jev_request_json、jev_response_json、policy_decision_json、latency_ms、skill_versions_json 列，
没有 BLOB 列或音频列。

机制（tech §7.1）：Store(path) 初始化时执行 SCHEMA 里的 CREATE TABLE（§6.8 七张表）；列类型只用 TEXT 和 INTEGER。
对新建的库跑 PRAGMA table_info(turns)。
"""

import sqlite3

import pytest

from waitless.store import Store

TABLES = ("profile", "standing_rules", "preferences", "companies", "tasks", "calls", "turns")
REQUIRED_TURN_COLUMNS = {"transcript", "jev_request_json", "jev_response_json", "policy_decision_json", "latency_ms",
                         "skill_versions_json"}


@pytest.fixture
def db(tmp_path):
    path = tmp_path / "schema.db"
    store = Store(path)
    store.init()
    store.close()
    con = sqlite3.connect(path)
    yield con
    con.close()


def test_ac33_turns_columns(db):
    cols = {row[1]: (row[2] or "").upper() for row in db.execute("PRAGMA table_info(turns)")}
    assert REQUIRED_TURN_COLUMNS <= set(cols)
    assert not [c for c, t in cols.items() if "BLOB" in t], "turns must not have BLOB columns"
    assert not [c for c in cols if "audio" in c.lower()], "turns must not have audio columns"


def test_ac33_all_seven_tables_exist(db):
    names = {row[0] for row in db.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    assert set(TABLES) <= names
