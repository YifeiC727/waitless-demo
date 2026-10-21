"""AC-87：text-sim 运行时不创建任何 Sonic 服务、音频传输或 WebSocket 连接，对方文字直接进决策门（可见组，冻结）。

第一组（10/14 晚文字版主线的前提）。被测：waitless/call/textcall.py 的 TextCall、create_text_call。
协作者全部是 e_support 的进程内替身（公司、决策门、trace、令牌），不依赖 A、B、G 的实现；
只有 test_ac87_menu_line_becomes_menu_turn 依赖 G 线的 decision/menu_parse.py 与 navigate-ivr 的 press_patterns。

两条子进程测试在新解释器里先装导入拦截器（pipecat、websockets、waitless.call.pipeline），
再导入 textcall 并跑一通电话；断言没有任何被拦下的导入，sys.modules 里也没有这些模块。
"""

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

import yaml

from e_support import (
    When,
    ScriptedCompany,
    build_settings,
    line,
    make_compiled,
    make_rig,
    make_task_card,
    queue_reactions,
    run_until,
    spec_end,
    spec_press,
    spec_wait,
)
from waitless import models as m

PROBE = Path(__file__).with_name("no_audio_probe.py")
ACCOUNT = "40012345"


def _probe(tmp_path: Path, mode_env, mode: str) -> dict:
    config = tmp_path / "probe-config.json"
    config.write_text(json.dumps({"env": mode_env("text-sim")}), encoding="utf-8")
    env = dict(os.environ)
    env["PYTHONDONTWRITEBYTECODE"] = "1"
    proc = subprocess.run(
        [sys.executable, "-B", str(PROBE), mode, str(config)],
        capture_output=True,
        text=True,
        timeout=180,
        env=env,
        cwd=str(tmp_path),
    )
    assert proc.returncode == 0, f"probe failed (exit {proc.returncode}):\n{proc.stderr[-4000:]}"
    return json.loads(proc.stdout.strip().splitlines()[-1])


def _rig(mode_env, engine_config, fake_clock, test_profile, company, script, compiled=None):
    settings = build_settings(mode_env("text-sim"), engine_config)
    return make_rig(
        settings=settings,
        clock=fake_clock,
        company=company,
        script=script,
        allowed_fields={**test_profile, "account_number": ACCOUNT},
        compiled=compiled,
    )


def test_ac87_importing_textcall_loads_no_audio_stack(tmp_path, mode_env):
    out = _probe(tmp_path, mode_env, "import")
    assert out["attempted"] == [], f"textcall tried to import {out['attempted']}"
    assert out["loaded"] == []


def test_ac87_text_call_runs_to_end_with_audio_stack_blocked(tmp_path, mode_env):
    out = _probe(tmp_path, mode_env, "run")
    assert out["attempted"] == [], f"a text-sim call tried to import {out['attempted']}"
    assert out["loaded"] == []
    assert out["ended"] is True
    assert out["digits"] == [ACCOUNT + "#"]
    assert "No thank you, please skip the survey." in out["heard"]
    assert out["line_tokens"] == 0, "a text call must not issue /ws/line tokens"
    assert len(out["ended_calls"]) == 1


async def test_ac87_each_company_line_goes_straight_to_decide_round(mode_env, engine_config, fake_clock, test_profile):
    greeting = "Thank you for calling Lakeshore Energy."
    ask = "Please enter your account number, followed by the pound key."
    after = "Thanks. Is there anything else I can help with?"
    company = ScriptedCompany(
        [line(greeting, role="ivr", kind="prompt"), line(ask)],
        queue_reactions(When("digits", ACCOUNT + "#", [line(after)])),
    )
    rig = _rig(
        mode_env,
        engine_config,
        fake_clock,
        test_profile,
        company,
        [("Thank you for calling", spec_wait()), ("account number", spec_press(ACCOUNT + "#")), ("anything else", spec_end())],
    )
    session = await rig.start()
    await run_until(fake_clock, lambda: session.ended, what="call end")

    turns = rig.gate.turns()
    assert [t.text for t in turns] == [greeting, ask, after]
    assert all(t.kind == "speech" and t.menu_options == [] for t in turns)
    assert all(t.call_id == rig.spec.call_id for t in turns)
    seqs = [t.seq for t in turns]
    assert seqs == sorted(seqs) and len(set(seqs)) == len(seqs)
    for turn, ctx in rig.gate.round_calls:
        assert ctx.call_id == rig.spec.call_id and ctx.task_id == rig.spec.task_id
        assert ctx.compiled == rig.compiled
        assert ctx.transcript[-1] == m.TranscriptLine(speaker="company", text=turn.text)
        assert ctx.context.allowed_fields == rig.compiled.allowed_fields
    assert [p for p, _ in rig.gate.point_calls if p == "call_round"] == [], "rounds go through decide_round only"
    assert company.digits() == [ACCOUNT + "#"]
    assert rig.line_tokens.issued == []
    assert rig.factory_calls == [rig.spec.scenario_id], "the company comes from deps.company_factory(spec.scenario_id)"
    assert session.scenario_id == rig.spec.scenario_id


async def test_ac87_company_turns_are_traced_with_turn_seq(mode_env, engine_config, fake_clock, test_profile):
    """A-08：每个 CompanyTurn 写一条 company_turn（t_ms = t_end_ms，action 带 kind 与 seq），
    dtmf_sent 指向的决策带 turn_seq，能追溯回这一轮。"""
    ask = "Please enter your account number, followed by the pound key."
    company = ScriptedCompany(
        [line(ask)],
        queue_reactions(When("digits", None, [line("Thank you. Goodbye.", kind="hangup")])),
    )
    rig = _rig(mode_env, engine_config, fake_clock, test_profile, company, [("account number", spec_press(ACCOUNT + "#"))])
    session = await rig.start()
    await run_until(fake_clock, lambda: session.ended, what="call end")

    (turn,) = rig.gate.turns()
    company_turns = [e for e in rig.events("company_turn") if (e.action or {}).get("seq") == turn.seq]
    assert len(company_turns) == 1, f"expected one company_turn for seq {turn.seq}: {rig.events('company_turn')}"
    ev = company_turns[0]
    assert ev.transcript == ask
    assert ev.speaker == "company"
    assert ev.t_ms == turn.t_end_ms
    assert (ev.action or {}).get("kind") == "speech"
    (dtmf,) = rig.events("dtmf_sent")
    decision = next(e for e in rig.events("decision") if e.decision_id == dtmf.decision_id)
    assert (decision.action or {}).get("turn_seq") == turn.seq


async def test_ac87_menu_line_becomes_menu_turn(mode_env, engine_config, fake_clock, test_profile, skills_dir):
    """菜单台词按 CompiledCall.press_patterns、digit_words 解析成 kind=menu 的 CompanyTurn（E-G-4）。
    依赖 G 线：parse_menu 的实现与 navigate-ivr 的 references/ivr-rules.yaml。"""
    rules = yaml.safe_load((skills_dir / "navigate-ivr" / "references" / "ivr-rules.yaml").read_text(encoding="utf-8"))
    compiled = make_compiled(
        make_task_card(),
        {**test_profile, "account_number": ACCOUNT},
        press_patterns=rules["press_patterns"],
        digit_words=rules.get("digit_words") or {},
    )
    menu = "Main menu. To start new service press 1. Press 2 for billing questions."
    company = ScriptedCompany(
        [line(menu, kind="menu", role="ivr", step_id="main_menu")],
        queue_reactions(When("digits", "1", [line("This option is not available. Goodbye.", kind="hangup", role="ivr")])),
    )
    rig = _rig(mode_env, engine_config, fake_clock, test_profile, company, [("Main menu", spec_press("1"))], compiled=compiled)
    session = await rig.start()
    await run_until(fake_clock, lambda: session.ended, what="call end")

    (turn,) = rig.gate.turns()
    assert turn.kind == "menu"
    assert turn.text == menu
    assert {"1", "2"} <= {o.digits for o in turn.menu_options}
    assert company.digits() == ["1"]


async def test_ac87_company_hangup_ends_call_without_another_decision(mode_env, engine_config, fake_clock, test_profile):
    company = ScriptedCompany([line("Our offices are closed. Goodbye.", kind="hangup", role="ivr")])
    rig = _rig(mode_env, engine_config, fake_clock, test_profile, company, [])
    session = await rig.start()
    await run_until(fake_clock, lambda: session.ended, what="call end after company hangup")
    await fake_clock.advance(1000)

    assert rig.gate.round_calls == []
    assert rig.ended_calls == [rig.spec.call_id]
    status = session.status()
    assert status.ended is True and status.phase == "ended"
    await session.wait_ended()
