"""AC-34：`waitless trace check <call_id>`：有后果事件（dtmf_sent、injection_sent、pending_choice_created、handoff_started、
hangup）找不到同一 decision_id 的 decision 事件（含 Jev 请求与返回、策略结果）时，退出码 1；trace 完整时退出码 0。

机制（tech §7.1）：Trace.check_call(call_id)（定稿 A-24：Trace 的方法）遍历一通电话的有后果事件，按 decision_id 找
decision 事件；cli.py 的 `waitless trace check` 据此给退出码。
定稿 A-25：「有后果事件必须带 decision_id」「decision 事件必须完整」在 Trace.event() 写入时检查，读回时不校验；
所以「不完整的 trace」用直接写 turns 行（§6.8 列名）的方式构造，模拟别的写入方或旧数据。
"""

import json
import sqlite3

import pytest

from waitless.cli import EXIT_CHECK_FAILED, EXIT_OK, main

from a_support import AEnv, company_turn, consequential, decision_event, plain

TASK = "task-34"


def complete_call(trace, call_id: str) -> None:
    """一通完整的电话：5 类有后果事件各一条，每条都有对应的 decision。"""
    t = iter(range(1_000, 100_000, 500))
    trace.event(plain("call_started", TASK, call_id, t_ms=next(t)))
    trace.event(company_turn(TASK, call_id, "For new service press 1.", kind="menu", turn_seq=0, t_ms=next(t)))
    trace.event(decision_event(TASK, call_id, f"{call_id}-d1", turn_seq=0, t_ms=next(t)))
    trace.event(consequential("dtmf_sent", TASK, call_id, f"{call_id}-d1", t_ms=next(t)))
    trace.event(company_turn(TASK, call_id, "Am I speaking with the account holder?", turn_seq=1, t_ms=next(t)))
    trace.event(decision_event(TASK, call_id, f"{call_id}-d2", turn_seq=1, t_ms=next(t)))
    trace.event(consequential("injection_sent", TASK, call_id, f"{call_id}-d2", t_ms=next(t)))
    trace.event(decision_event(TASK, call_id, f"{call_id}-d3", turn_seq=2, t_ms=next(t)))
    trace.event(consequential("pending_choice_created", TASK, call_id, f"{call_id}-d3", t_ms=next(t)))
    trace.event(decision_event(TASK, call_id, f"{call_id}-d4", turn_seq=3, t_ms=next(t)))
    trace.event(plain("handoff_ready", TASK, call_id, t_ms=next(t)))
    trace.event(consequential("handoff_started", TASK, call_id, f"{call_id}-d4", t_ms=next(t)))
    trace.event(plain("handoff_ended", TASK, call_id, t_ms=next(t)))
    trace.event(decision_event(TASK, call_id, f"{call_id}-d5", turn_seq=4, t_ms=next(t)))
    trace.event(consequential("hangup", TASK, call_id, f"{call_id}-d5", t_ms=next(t)))
    trace.event(plain("call_ended", TASK, call_id, t_ms=next(t)))


def raw_turn(db_path, **cols) -> None:
    """直接写一行 turns（绕过 Trace.event 的写入检查）。"""
    con = sqlite3.connect(db_path)
    try:
        nxt = con.execute("SELECT COALESCE(MAX(seq), 0) + 1 FROM turns").fetchone()[0]
        cols.setdefault("seq", nxt)
        cols.setdefault("t_ms", 200_000)
        names = ", ".join(cols)
        marks = ", ".join("?" for _ in cols)
        con.execute(f"INSERT INTO turns ({names}) VALUES ({marks})", tuple(cols.values()))
        con.commit()
    finally:
        con.close()


@pytest.fixture
def env(tmp_path):
    e = AEnv(tmp_path, mode="replay")
    yield e
    e.close()


def test_ac34_complete_trace_has_no_violations(env):
    complete_call(env.trace, "call-ok")
    assert env.trace.check_call("call-ok") == []


def test_ac34_consequential_event_without_its_decision_is_a_violation(env):
    complete_call(env.trace, "call-bad")
    raw_turn(env.settings.db_path, task_id=TASK, call_id="call-bad", event_type="dtmf_sent",
             decision_id="dec-never-written", action_json=json.dumps({"digits": "9"}))
    violations = env.trace.check_call("call-bad")
    assert len(violations) == 1
    v = violations[0]
    assert v.event_type == "dtmf_sent"
    assert v.decision_id == "dec-never-written"
    assert v.reason.strip()


def _cli_env(monkeypatch, mode_env, env) -> None:
    values = mode_env("replay",
                      WAITLESS_DB_PATH=str(env.settings.db_path),
                      WAITLESS_WEB_DIR=str(env.settings.web_dir),
                      WAITLESS_SKILLS_DIR=str(env.settings.skills_dir),
                      WAITLESS_COMPANY_DIR=str(env.settings.company_dir),
                      WAITLESS_REPLAY_DIR=str(env.settings.replay_dir))
    for name, value in values.items():
        monkeypatch.setenv(name, value)


def test_ac34_cli_exit_codes(env, monkeypatch, mode_env):
    complete_call(env.trace, "call-ok")
    complete_call(env.trace, "call-bad")
    raw_turn(env.settings.db_path, task_id=TASK, call_id="call-bad", event_type="injection_sent",
             decision_id="dec-missing", action_json=json.dumps({"text": "Please hold.", "template": None}))
    _cli_env(monkeypatch, mode_env, env)
    assert main(["trace", "check", "call-ok"]) == EXIT_OK == 0
    assert main(["trace", "check", "call-bad"]) == EXIT_CHECK_FAILED == 1
