"""AC-96〔需密钥〕：`waitless check live` 的同一份报告里，菜单结束到按键音发出的 p50 ≤4000 ms。

机制（tech §7.1，I-2 执行，A 线写代码）：check_live(scenario, settings) 跑完一通 live-sim 电话后读 turns 表：对每条
dtmf_sent，按 decision_id 找到 decision，再按 decision 的 turn_seq 找到那一轮的 company_turn（定稿 A-08），
用 dtmf_sent.t_ms 减 company_turn.t_ms，取全部差值的 p50 写进报告。
本测试默认不跑（needs_keys）；由 I-2 经 `scripts/load-keys-macos.sh` 显式选中：

    cd server && ../scripts/load-keys-macos.sh uv run pytest -m needs_keys tests/test_live_check.py

被显式选中而缺密钥或缺配置时，load_settings 抛 ConfigError，测试失败（不跳过）。
等价的命令行检查与通过条件写在 RUN/step3/tests/A/test-plan.md 第 1 节。
离线可测的计算部分见 test_trace_metrics.py::test_ac96_menu_to_dtmf_ms_from_trace。

r3 缺口 X-G1（定稿 self_contained）：check live 自己起、关模拟公司进程，所以它除 live-sim 的必需变量外还要
REQUIRED_FOR_COMPANY（WAITLESS_COMPANY_HOST/PORT）。下面两条默认测试只查配置这一步：缺任一项退出码 2、stderr 点名，
并且在报错之前不起任何子进程、不拨号。
"""

import asyncio
import os
import statistics
import subprocess

import pytest

from waitless.cli import EXIT_CONFIG, check_live, main
from waitless.config import REQUIRED_FOR_COMPANY, load_settings

from a_support import ENGINE_YAML, make_dirs


@pytest.mark.needs_keys
async def test_ac96_live_menu_to_dtmf_p50_within_4000_ms():
    settings = load_settings(os.environ)  # 缺任一必需变量即 ConfigError → 失败
    assert settings.mode == "live-sim", "AC-96 runs against live-sim (real audio)"
    report = await check_live("baseline", settings)
    assert report.scenario_id == "baseline"
    assert report.menu_to_dtmf_ms, "baseline has two menus; at least one menu→dtmf delay must be measured"
    assert all(ms >= 0 for ms in report.menu_to_dtmf_ms)
    assert report.menu_to_dtmf_ms_p50 == pytest.approx(statistics.median(report.menu_to_dtmf_ms))
    assert report.menu_to_dtmf_ms_p50 <= 4000



def _no_launch(monkeypatch):
    def refuse(*args, **kwargs):
        raise AssertionError("check live must not launch anything before its configuration is complete")

    monkeypatch.setattr(subprocess, "Popen", refuse)
    monkeypatch.setattr(subprocess, "run", refuse)
    monkeypatch.setattr(asyncio, "create_subprocess_exec", refuse)


@pytest.mark.parametrize("var", [*REQUIRED_FOR_COMPANY, "AWS_ACCESS_KEY_ID", "WAITLESS_CARRIER_URL"])
def test_ac96_check_live_config_names_missing_variable(mode_env, tmp_path, monkeypatch, capfd, var):
    dirs = make_dirs(tmp_path / "p")
    env = mode_env("live-sim", WAITLESS_WEB_DIR=str(dirs["web"]), WAITLESS_SKILLS_DIR=str(dirs["skills"]),
                   WAITLESS_COMPANY_DIR=str(dirs["company"]), WAITLESS_ENGINE_CONFIG=str(ENGINE_YAML),
                   WAITLESS_COMPANY_HOST="127.0.0.1", WAITLESS_COMPANY_PORT="0")
    env.pop(var)
    for name in [*env, var, "WAITLESS_PLANNER", "WAITLESS_REPLAY_DIR"]:
        monkeypatch.delenv(name, raising=False)
    for name, value in env.items():
        monkeypatch.setenv(name, value)
    _no_launch(monkeypatch)
    code = main(["check", "live", "--scenario", "baseline", "--report", str(tmp_path / "report.json")])
    assert code == EXIT_CONFIG
    assert var in capfd.readouterr().err
    assert not (tmp_path / "report.json").exists()
