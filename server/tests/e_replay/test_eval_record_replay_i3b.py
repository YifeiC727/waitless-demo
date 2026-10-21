"""I-3b 补测 · run_eval 的录制与回放（第 3 步可见组，冻结）：AC-88、AC-89（test-gaps G13、G14、G15、G16、G18、G19）。

按 I3B 裁决（RUN/lanes/i3b/i3b-decisions.md）：
- D6：录制归 run_eval——它在组装服务前清空 settings.record_dir，只按自己的 record_dir 参数录，菜单、用例、场景电话在同一份文件里；
  文件名 <scenario_id>.jsonl，同名已存在就报错、不覆盖（X-G4）；task_id、call_id 存成占位符。
- D7：WAITLESS_PLANNER=codex 时给了 record_dir 就在发任何请求之前报错，不产生文件（D14：正式回放必须是 Nova 2 Lite）。
- D10：回放跑完还剩记录也报 ReplayMismatch（过期回放不能悄悄通过）。
回放那次不注入任何客户端（run_eval 自建 ReplayPlayer）；conftest 已清掉密钥并拦住一切出站连接，改走在线只会得到别的错误。
"""

import json

import pytest

from e_support.evalkit import (
    MultiOracle,
    all_ids,
    calls_rows,
    eval_with,
    fast_engine,
    replay_eval,
    replay_settings,
    scenario_case,
    text_settings,
    with_answers,
    write_menus,
)
from waitless import models as m
from waitless.interfaces import ReplayMismatch


async def record_baseline(tmp_path, mode_env, engine_config, company_dir, skills_dir, *, env_record_dir=None):
    """text-sim 录一份 baseline：15 个菜单（第 4 个答错）+ baseline 场景电话。返回 (文件, 原文, 指标, settings, 菜单目录, 用例)。"""
    oracle = MultiOracle(company_dir)
    menus, answers = write_menus(tmp_path / "menus", wrong={4})
    cases = [scenario_case("baseline")]
    settings = text_settings(mode_env, fast_engine(engine_config), tmp_path / "record")
    if env_record_dir is not None:
        settings = settings.model_copy(update={"record_dir": env_record_dir})
    rec = tmp_path / "rec"
    metrics = await eval_with(with_answers(oracle.answer_jev, by_text=answers), oracle.answer_converse, mode="text-sim",
                              settings=settings, skills_dir=skills_dir, menus_dir=menus, cases=cases, scenario="baseline", record_dir=rec)
    path = rec / "baseline.jsonl"
    return path, path.read_text(encoding="utf-8"), metrics, settings, menus, cases


async def test_ac88_i3b_run_eval_records_one_file_with_header_and_placeholders(tmp_path, mode_env, engine_config, company_dir, skills_dir):
    from waitless.skills.loader import load_skills

    env_rec = tmp_path / "env-rec"
    path, raw, _, settings, _, cases = await record_baseline(tmp_path, mode_env, engine_config, company_dir, skills_dir, env_record_dir=env_rec)
    assert path.is_file()
    assert not env_rec.exists() or not any(env_rec.iterdir()), "settings.record_dir must not start a second recording"
    lines = [x for x in raw.splitlines() if x.strip()]
    header = m.ReplayHeader.model_validate_json(lines[0])
    assert (header.source_mode, header.scenario_id) == ("text-sim", "baseline")
    assert header.request_text == cases[0].input["request"]
    assert header.models.jev == settings.jev_model
    assert header.models.planner == f"bedrock:{settings.planner_model_id}"
    assert {s.name for s in header.skills} == set(load_skills(skills_dir))
    records = [m.ReplayRecord.model_validate_json(x) for x in lines[1:]]
    assert sum(r.channel == "jev" for r in records) >= 15 + 1, "the menu rounds and the call's rounds are all in this one file"
    assert any(r.channel.startswith("converse:") for r in records)
    ids = all_ids(settings.db_path)
    assert ids and not any(i in raw for i in ids), "task_id and call_id are stored as placeholders"


async def test_ac88_i3b_recording_never_overwrites_an_existing_file(tmp_path, mode_env, engine_config, company_dir, skills_dir):
    path, raw, _, _, menus, cases = await record_baseline(tmp_path, mode_env, engine_config, company_dir, skills_dir)
    oracle = MultiOracle(company_dir)
    again = text_settings(mode_env, fast_engine(engine_config), tmp_path / "again")
    with pytest.raises(FileExistsError):
        await eval_with(oracle.answer_jev, oracle.answer_converse, mode="text-sim", settings=again, skills_dir=skills_dir,
                        menus_dir=menus, cases=cases, scenario="baseline", record_dir=path.parent)
    assert path.read_text(encoding="utf-8") == raw
    assert not again.db_path.exists() or calls_rows(again.db_path) == {}, "refused before any call was placed"


async def test_ac88_i3b_recording_refuses_the_codex_planner(tmp_path, mode_env, engine_config, company_dir, skills_dir):
    from fakes import FakeConverse, FakeJev
    from waitless.config import ConfigError
    from waitless.evals.runner import run_eval

    oracle = MultiOracle(company_dir)
    menus, _ = write_menus(tmp_path / "menus")
    settings = text_settings(mode_env, fast_engine(engine_config), tmp_path / "codex").model_copy(
        update={"planner": "codex", "codex_command": ("codex", "exec")}
    )
    jev, converse = FakeJev(default=oracle.answer_jev), FakeConverse(default=oracle.answer_converse)
    with pytest.raises((ValueError, ConfigError)):
        await run_eval("text-sim", settings=settings, skills_dir=skills_dir, menus_dir=menus, cases=[scenario_case("baseline")],
                       scenario="baseline", record_dir=tmp_path / "rec", jev=jev, converse=converse)
    assert not (tmp_path / "rec" / "baseline.jsonl").exists()
    assert jev.calls == [] and converse.calls == [], "refused before any request was sent"


async def test_ac88_i3b_replay_reproduces_the_recorded_metrics_offline(tmp_path, mode_env, engine_config, company_dir, skills_dir):
    path, raw, recorded, settings, menus, cases = await record_baseline(tmp_path, mode_env, engine_config, company_dir, skills_dir)
    rs = replay_settings(mode_env, fast_engine(engine_config), tmp_path / "replay", path.parent, path.name)
    assert rs.mode == "replay" and rs.jev_api_key is None and rs.aws_access_key_id is None
    replayed = await replay_eval(rs, skills_dir=skills_dir, menus_dir=menus, cases=cases)
    assert replayed == recorded
    assert recorded.menu_accuracy == pytest.approx(14 / 15)
    assert calls_rows(rs.db_path) == calls_rows(settings.db_path) == {"baseline": ("confirmed", 1)}
    assert path.read_text(encoding="utf-8") == raw, "replay never rewrites the file"


async def test_ac89_i3b_replay_running_out_mid_eval_raises_mismatch(tmp_path, mode_env, engine_config, company_dir, skills_dir):
    path, raw, _, _, menus, cases = await record_baseline(tmp_path, mode_env, engine_config, company_dir, skills_dir)
    lines = [x for x in raw.splitlines() if x.strip()]
    cut = tmp_path / "trunc"
    cut.mkdir()
    (cut / "baseline.jsonl").write_text("\n".join(lines[: 1 + (len(lines) - 1) // 2]) + "\n", encoding="utf-8")
    rs = replay_settings(mode_env, fast_engine(engine_config), tmp_path / "t", cut, "baseline.jsonl")
    with pytest.raises(ReplayMismatch) as err:
        await replay_eval(rs, skills_dir=skills_dir, menus_dir=menus, cases=cases)
    assert err.value.channel and err.value.seq >= 0


async def test_ac89_i3b_replay_with_leftover_records_raises_mismatch(tmp_path, mode_env, engine_config, company_dir, skills_dir):
    path, raw, _, _, menus, cases = await record_baseline(tmp_path, mode_env, engine_config, company_dir, skills_dir)
    lines = [x for x in raw.splitlines() if x.strip()]
    extra = json.loads(lines[-1])
    same_channel = [json.loads(x) for x in lines[1:] if json.loads(x)["channel"] == extra["channel"]]
    extra["seq"] = max(r["seq"] for r in same_channel) + 1
    left = tmp_path / "leftover"
    left.mkdir()
    (left / "baseline.jsonl").write_text(raw.rstrip("\n") + "\n" + json.dumps(extra) + "\n", encoding="utf-8")
    rs = replay_settings(mode_env, fast_engine(engine_config), tmp_path / "l", left, "baseline.jsonl")
    with pytest.raises(ReplayMismatch) as err:
        await replay_eval(rs, skills_dir=skills_dir, menus_dir=menus, cases=cases)
    assert (err.value.channel, err.value.seq) == (extra["channel"], extra["seq"])
