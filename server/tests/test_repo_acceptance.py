"""在真实仓库上跑的验收检查（第 3 步写，冻结；tech §7.1、§9 的 I-2、I-3、I-4、§12）。

默认测试（零密钥、零网络）：
  AC-02  没有第二套前端（app/、APK 工作流、web/ 里的 Capacitor）。
  AC-17、AC-31、AC-100、AC-102、AC-103、AC-104  对本仓库跑 scripts/checks.sh 的对应子命令，退出码必须是 0。
    这几条在集成收尾（I-4）之前会是红的：README.md 仍是原型说明、FRICTION-LOG.md 还没有、web/ 还没改完。
需要显式选择（-m）的集成检查，缺密钥、缺网络或缺必需的环境变量时失败，不跳过：
  AC-95、AC-97  needs_keys：`waitless check live --scenario baseline --report …`（I-2）。
  AC-98         needs_device：模拟公司的统计文件（WAITLESS_COMPANY_STATS 指的 JSONL）里，最后一通的转接期间非静音入帧数 > 0
                （I-3 真机检查单的机器可查部分；缺口 X-G6，定稿 stats_jsonl，暂定）。
  AC-99         needs_network：新克隆仓库，逐条执行 README 回放段落的命令，打开首页（I-4）。
  AC-105        needs_network：远端 main 仍是 871eb23，本 run 的提交只在 feat/real-call-engine 与 lane/* 上（I-4）。
AC-106（用户查 AWS 账单）没有程序检查，见 RUN/step3/tests/X/test-plan.md 的检查单。

这些检查的输入（报告路径、统计文件、commit 列表）由操作者用 WAITLESS_* 变量给出；conftest 的零密钥守卫会在每个
非 needs_keys 测试开始前删掉进程里的 WAITLESS_* 变量，所以这里在模块导入时（收集阶段，守卫生效之前）先存一份 ENV_AT_IMPORT。
"""

from __future__ import annotations

import json
import os
import re
import signal
import subprocess
import time
from pathlib import Path
from typing import Any

import pytest
import yaml

CHECKS_REL = Path("scripts") / "checks.sh"
MAIN_SHA = "871eb23b02c93bc02872086dec340214885e6347"  # PRD AC-105、C13：原型 main 的提交
FEAT_BRANCH = "feat/real-call-engine"
COMPANY_ID = "lakeshore-energy"
CALL_SID_RE = re.compile(r"^CA[0-9a-fA-F]{32}$")
URL_RE = re.compile(r"https?://[^\s'\"<>]+")
COMMAND_LIMIT_S = 900
README_REPLAY_LINES = ["cp .env.replay .env", "scripts/run.sh replay"]  # 缺口 X-G7（定稿 committed_replay_env，暂定）
ENV_AT_IMPORT: dict[str, str] = dict(os.environ)
SERVER_URL_LIMIT_S = 180


def run_checks(repo_root: Path, sub: str, *args: str) -> subprocess.CompletedProcess[str]:
    script = repo_root / CHECKS_REL
    assert script.is_file(), f"{script} is missing"
    return subprocess.run(["bash", str(script), sub, *args], capture_output=True, text=True, timeout=300)


def assert_check_passes(repo_root: Path, sub: str, *args: str) -> None:
    proc = run_checks(repo_root, sub, *args)
    assert proc.returncode == 0, f"scripts/checks.sh {sub} exited {proc.returncode}\n{proc.stdout}{proc.stderr}"


def git(repo_root: Path, *args: str, timeout: int = 300) -> str:
    proc = subprocess.run(["git", "-C", str(repo_root), *args], capture_output=True, text=True, timeout=timeout)
    assert proc.returncode == 0, f"git {' '.join(args)} failed ({proc.returncode}): {proc.stderr}"
    return proc.stdout


# ---------------------------------------------------------------------------
# AC-02
# ---------------------------------------------------------------------------


def test_ac02_no_second_frontend(repo_root: Path) -> None:
    """PRD 检查：test ! -e app && test ! -e .github/workflows/android-apk.yml && ! grep -rqi capacitor web"""
    assert not (repo_root / "app").exists(), "app/ (Capacitor shell) must be gone (D9)"
    assert not (repo_root / ".github" / "workflows" / "android-apk.yml").exists(), "APK workflow must be gone (D9)"
    web = repo_root / "web"
    hits = [
        str(p.relative_to(repo_root))
        for p in (web.rglob("*") if web.is_dir() else [])
        if p.is_file() and b"capacitor" in p.read_bytes().lower()
    ]
    assert hits == [], f"web/ mentions Capacitor: {hits}"


# ---------------------------------------------------------------------------
# 静态检查在真实仓库上（AC-17、AC-31、AC-100、AC-102、AC-103、AC-104）
# ---------------------------------------------------------------------------


def test_ac17_repo_trademarks_check_passes(repo_root: Path) -> None:
    assert_check_passes(repo_root, "trademarks")


def test_ac31_repo_services_check_passes(repo_root: Path) -> None:
    assert_check_passes(repo_root, "services")


def test_ac100_repo_audio_check_passes(repo_root: Path) -> None:
    assert_check_passes(repo_root, "audio")


def test_ac102_repo_hardcoded_check_passes(repo_root: Path) -> None:
    assert_check_passes(repo_root, "hardcoded")


def test_ac103_repo_secrets_check_passes(repo_root: Path) -> None:
    assert_check_passes(repo_root, "secrets")


def test_ac104_repo_friction_log_complete(repo_root: Path) -> None:
    assert_check_passes(repo_root, "friction")


# ---------------------------------------------------------------------------
# AC-95、AC-97：需密钥的真实音频主线（I-2）
# ---------------------------------------------------------------------------


@pytest.fixture(scope="module")
def live_report(tmp_path_factory: pytest.TempPathFactory) -> dict[str, Any]:
    """跑一次 `waitless check live --scenario baseline --report <tmp>`，AC-95 与 AC-97 共用。
    check live 自己起、关模拟公司进程（缺口 X-G1），所以一条命令即可；env 里要有 live-sim 与 `waitless company` 的变量。

    I-2 已经按 PRD 的命令跑过时，把 WAITLESS_CHECK_LIVE_REPORT 设成那份报告（.run/check-baseline.json），这里只读不重跑。
    """
    given = ENV_AT_IMPORT.get("WAITLESS_CHECK_LIVE_REPORT")
    if given:
        path = Path(given)
        assert path.is_file(), f"WAITLESS_CHECK_LIVE_REPORT points to a missing file: {path}"
        return {"rc": 0, "report": json.loads(path.read_text(encoding="utf-8")), "ran": False}
    from waitless import cli

    path = tmp_path_factory.mktemp("check-live") / "check-baseline.json"
    rc = cli.main(["check", "live", "--scenario", "baseline", "--report", str(path)])
    assert path.is_file(), f"`waitless check live` exited {rc} and wrote no report"
    return {"rc": rc, "report": json.loads(path.read_text(encoding="utf-8")), "ran": True}


@pytest.mark.needs_keys
def test_ac95_check_live_baseline_confirmed(live_report: dict[str, Any], company_dir: Path) -> None:
    from waitless import cli
    from waitless.models import Scenario

    report = live_report["report"]
    assert live_report["rc"] == 0, f"check live exited {live_report['rc']}: {report}"
    cli.LiveCheckReport.model_validate(report)  # --report 写出的 JSON 就是 LiveCheckReport（骨架 r2 的字段名）
    assert report["scenario_id"] == "baseline"
    assert report["trace_ok"] is True, report
    scenario_path = company_dir / COMPANY_ID / "scenarios" / "baseline.yaml"
    scenario = Scenario.model_validate(yaml.safe_load(scenario_path.read_text(encoding="utf-8")))
    assert report["outcome"] == report["expected_outcome"] == scenario.expected.outcome, report
    number = report["confirmation_number"]
    if report["outcome"] == "confirmed":
        assert report["confirmation_matches"] or (report["result_source"] == "user" and number is None), report
    if number is not None:
        assert isinstance(number, str) and re.fullmatch(scenario.expected.confirmation_pattern, number), (
            f"confirmation number {number!r} does not match {scenario.expected.confirmation_pattern}"
        )
    # trace check 另跑一次（同一个 WAITLESS_DB_PATH），不只信报告里的布尔值
    assert cli.main(["trace", "check", report["call_id"]]) == 0


@pytest.mark.needs_keys
def test_ac97_check_live_identity_check_pass(live_report: dict[str, Any]) -> None:
    from waitless.cli import LiveCheckReport

    report = live_report["report"]
    LiveCheckReport.model_validate(report)
    assert report.get("identity_check") == "pass", report
    assert report.get("identity_utterance") is None, report


# ---------------------------------------------------------------------------
# AC-98：真机检查单里机器可查的一项（I-3）
# ---------------------------------------------------------------------------


@pytest.mark.needs_device
def test_ac98_company_stats_show_user_audio_during_handoff() -> None:
    """WAITLESS_COMPANY_STATS：I-3 真机运行时给模拟公司设的统计文件（每通结束追加一行
    {"call_sid", "nonsilent_inbound_frames_during_handoff"}；缺口 X-G6，暂定）。检查单要求用一份新文件，
    真机那一通是最后一行。"""
    stats = ENV_AT_IMPORT.get("WAITLESS_COMPANY_STATS")
    assert stats, "set WAITLESS_COMPANY_STATS to the stats file the company process wrote during the phone run"
    path = Path(stats)
    assert path.is_file(), f"{path} does not exist: was WAITLESS_COMPANY_STATS set for the company process?"
    rows = [json.loads(ln) for ln in path.read_text(encoding="utf-8").splitlines() if ln.strip()]
    assert rows, f"{path} has no call rows"
    for row in rows:
        from tests.lane_B.i19b_stats_support import assert_schema
        assert_schema(row)
        assert CALL_SID_RE.fullmatch(row["call_sid"]), row
        count = row["nonsilent_inbound_frames_during_handoff"]
        assert isinstance(count, int) and not isinstance(count, bool) and count >= 0, row
    last = rows[-1]
    assert last["nonsilent_inbound_frames_during_handoff"] > 0, f"no non-silent user audio during the handoff: {last}"


# ---------------------------------------------------------------------------
# AC-99：新克隆照 README 回放段落执行（I-4）
# ---------------------------------------------------------------------------

_HEADING = re.compile(r"^(#{1,6})\s+(.*)$")
_FENCE = re.compile(r"^\s*```\s*([A-Za-z]*)\s*$")


def readme_replay_commands(text: str) -> list[str]:
    """README 里标题含 replay / 回放 的那一节（到同级或更高级标题为止）中，bash/sh/shell/无语言 代码块的每一行命令。"""
    commands: list[str] = []
    level: int | None = None
    in_fence = False
    fence_ok = False
    for line in text.splitlines():
        fence = _FENCE.match(line)
        if fence:
            if in_fence:
                in_fence = False
            else:
                in_fence = True
                fence_ok = fence.group(1).lower() in ("", "bash", "sh", "shell", "console", "zsh")
            continue
        if in_fence:
            if level is not None and fence_ok:
                cmd = line.strip()
                if cmd.startswith("$ "):
                    cmd = cmd[2:]
                if cmd and not cmd.startswith("#"):
                    commands.append(cmd)
            continue
        heading = _HEADING.match(line)
        if heading:
            depth = len(heading.group(1))
            if level is not None and depth <= level:
                break
            if level is None and re.search(r"replay|回放", heading.group(2), re.IGNORECASE):
                level = depth
    return commands


def _is_server_command(cmd: str) -> bool:
    return bool(re.search(r"(^|[\s/])run\.sh(\s|$)|waitless\s+serve", cmd))


def _is_browser_command(cmd: str) -> bool:
    return bool(re.match(r"(open|xdg-open|start)\s", cmd))


@pytest.mark.needs_network
def test_ac99_fresh_clone_runs_readme_replay_section(repo_root: Path, tmp_path: Path) -> None:
    """评审的电脑：没有任何 WAITLESS_* / AWS_* 变量；只有 README 回放段落给的命令。

    建议运行：uv run pytest -m needs_network tests/test_repo_acceptance.py -k ac99 --basetemp=/private/tmp/waitless-ac99
    """
    import httpx

    url = git(repo_root, "remote", "get-url", "origin").strip()
    branch = git(repo_root, "rev-parse", "--abbrev-ref", "HEAD").strip()
    clone = tmp_path / "fresh-clone"
    proc = subprocess.run(
        ["git", "clone", "--depth", "1", "--branch", branch, url, str(clone)],
        capture_output=True,
        text=True,
        timeout=COMMAND_LIMIT_S,
    )
    assert proc.returncode == 0, proc.stderr
    commands = readme_replay_commands((clone / "README.md").read_text(encoding="utf-8"))
    assert commands, "README.md has no replay section (heading with 'replay' or '回放') with shell code blocks"
    assert commands == README_REPLAY_LINES, f"X-G7: the replay section should be exactly {README_REPLAY_LINES}, got {commands}"
    assert any(_is_server_command(c) for c in commands), "the replay section never starts the server (scripts/run.sh)"

    env = {k: v for k, v in os.environ.items() if not (k.startswith("WAITLESS_") or k.startswith("AWS_"))}
    server_log = tmp_path / "server.log"
    lines = ["set -o pipefail", f"cd {json.dumps(str(clone))}"]
    for i, cmd in enumerate(commands):
        if _is_browser_command(cmd):
            continue
        if _is_server_command(cmd):
            lines.append(f"( {cmd} ) > {json.dumps(str(server_log))} 2>&1 &")
            lines.append(f'echo "@@server {i} $!"')
            continue
        lines.append(cmd)
        lines.append(f'__rc=$?; echo "@@rc {i} $__rc"; [ "$__rc" -eq 0 ] || exit 90')
    driver = subprocess.Popen(
        ["bash", "-c", "\n".join(lines)],
        env=env,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        start_new_session=True,
    )
    try:
        out, _ = driver.communicate(timeout=COMMAND_LIMIT_S * max(1, len(commands)))
        rcs = re.findall(r"^@@rc (\d+) (\d+)$", out, re.MULTILINE)
        failed = [(commands[int(i)], rc) for i, rc in rcs if rc != "0"]
        assert driver.returncode == 0 and not failed, f"README replay commands failed: {failed}\n{out[-4000:]}"
        assert "@@server" in out, out[-4000:]
        deadline = time.monotonic() + SERVER_URL_LIMIT_S
        page = None
        while time.monotonic() < deadline:
            text = server_log.read_text(encoding="utf-8", errors="replace") if server_log.exists() else ""
            found = URL_RE.search(text)
            if found:
                try:
                    page = httpx.get(found.group(0).rstrip(".,;)"), timeout=5.0)
                    if page.status_code == 200:
                        break
                except httpx.HTTPError:
                    pass
            time.sleep(0.5)
        assert page is not None and page.status_code == 200, f"home page not reachable\n{server_log.read_text() if server_log.exists() else ''}"
        assert page.headers.get("content-type", "").startswith("text/html")
    finally:
        try:
            os.killpg(driver.pid, signal.SIGTERM)
        except ProcessLookupError:
            pass


# ---------------------------------------------------------------------------
# AC-105：main 未动，提交只在 feat 与 lane/*（I-4）
# ---------------------------------------------------------------------------


@pytest.mark.needs_network
def test_ac105_remote_main_untouched_and_run_commits_only_on_feat_or_lane(repo_root: Path) -> None:
    """可选 WAITLESS_RUN_COMMITS：各线交回报告里记下的 commit（空格分隔），与分支上找到的一起检查。"""
    remote_main = git(repo_root, "ls-remote", "origin", "refs/heads/main").split()
    assert remote_main and remote_main[0] == MAIN_SHA, f"origin main is {remote_main[:1]}, expected {MAIN_SHA}"
    git(repo_root, "fetch", "--prune", "origin")
    heads = git(repo_root, "for-each-ref", "--format=%(refname:short)", "refs/remotes/origin").split()
    run_heads = [h for h in heads if h == f"origin/{FEAT_BRANCH}" or h.startswith("origin/lane/")]
    assert f"origin/{FEAT_BRANCH}" in run_heads, f"origin/{FEAT_BRANCH} not found in {heads}"
    commits: set[str] = set()
    for head in run_heads:
        commits.update(git(repo_root, "rev-list", head, "--not", MAIN_SHA).split())
    for c in ENV_AT_IMPORT.get("WAITLESS_RUN_COMMITS", "").split():
        commits.add(git(repo_root, "rev-parse", f"{c}^{{commit}}").strip())
    assert commits, "no run commits found on feat/real-call-engine or lane/*"
    bad: dict[str, list[str]] = {}
    for c in sorted(commits):
        # 「origin/HEAD -> origin/x」是符号引用，按它指向的分支算（指向 main 就是在 main 上）
        lines = [
            ln.strip().split(" -> ")[-1]
            for ln in git(repo_root, "branch", "-r", "--contains", c).splitlines()
            if ln.strip()
        ]
        wrong = [ln for ln in lines if not (ln == f"origin/{FEAT_BRANCH}" or ln.startswith("origin/lane/"))]
        if wrong or not lines:
            bad[c] = lines
    assert not bad, f"run commits found outside feat/lane branches: {bad}"
