"""scripts/checks.sh 的行为（第 3 步写，冻结；AC-17、AC-31、AC-100、AC-102、AC-103、AC-104；tech §5.4、§7.1、§12）。

每个测试在 tmp_path 里搭一个最小「仓库」：复制本仓库的 scripts/checks.sh，写入干净样例，git init + git add，
再埋入一种问题，从仓库外的目录调用 checks.sh，断言退出码（0 通过、1 有问题、2 用法或环境错误）和打印的 文件:行。
真实仓库上的同名检查在 test_repo_acceptance.py。

本文件里的商标词和密钥样式字符串都在运行时拼出来，源码里没有完整字面量，
否则 `checks.sh trademarks` / `checks.sh secrets` 在真实仓库上会命中本文件。
"""

from __future__ import annotations

import re
import shutil
import subprocess
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

import pytest

CHECKS_REL = Path("scripts") / "checks.sh"
SUBCOMMANDS = ("trademarks", "services", "audio", "hardcoded", "secrets", "friction")

# --- 运行时拼出的样例（源码里不出现完整字面量） ----------------------------------
BRAND_WAREHOUSE = "Cost" + "co"
BRAND_BANK = "Bank of " + "America"
BRAND_BANK_SHORT = "Bo" + "A"
FAKE_ACCESS_KEY_ID = "AK" + "IA" + "TESTONLYEXAMPLE7"  # 4 + 16 位，AC-103 的访问密钥样式
FAKE_SECRET_40 = "wJalrXUtnFEMI/K7MDENG" + "/bPxRfiCYEXAMPLEKEY"  # 40 位
FAKE_BEARER = "abc123DEF456" + "ghi789JKL0mn"
PRIVATE_KEY_HEAD = "-----BEGIN " + "RSA PRIVATE KEY-----"

CLEAN_FILES: dict[str, str] = {
    "README.md": "# Waitless\n\nRun the replay demo with scripts/run.sh replay.\n",
    "server/pyproject.toml": (
        "[project]\n"
        'name = "waitless"\n'
        "# no telephony or storage SDKs here (AC-31)\n"
        "dependencies = [\n"
        '  "pipecat-ai[aws-nova-sonic,websocket]==1.12.0",\n'
        '  "mcp==2.3.0",\n'
        '  "boto3==1.43.112",\n'
        "]\n"
    ),
    "server/uv.lock": (
        "version = 1\n"
        '[[package]]\nname = "boto3"\nversion = "1.43.112"\n'
        '[[package]]\nname = "s3transfer"\nversion = "0.14.0"\n'
        '[[package]]\nname = "botocore"\nversion = "1.43.112"\n'
    ),
    "server/waitless/__init__.py": '__version__ = "0.1.0"\n',
    "server/waitless/bedrock.py": (
        "import boto3\n\n\n"
        "def make_client(settings):\n"
        '    return boto3.client("bedrock-runtime", region_name=settings.aws_region)\n'
    ),
    "server/waitless/company/ivr_audio.py": (
        "import boto3\n\n"
        "DTMF_ROWS_HZ = (697.0, 770.0, 852.0, 941.0)\n"
        "DTMF_COLS_HZ = (1209.0, 1336.0, 1477.0)\n"
        'MEDIA_FORMAT = {"encoding": "audio/x-mulaw", "sampleRate":8000, "channels": 1}\n'
        'CLOSE = {"code":4401}\n'
        "# pipecat reference: llm.py:1501-1569\n\n\n"
        "def polly(region):\n"
        '    return boto3.Session(region_name=region).client("polly")\n\n\n'
        "def first_chunk(payload):\n"
        "    return payload[:3200]\n"
    ),
    "server/waitless/call/line.py": (
        'LINE_PATH = "/ws/line"\n'
        "def stream_url(settings):\n"
        '    return settings.line_ws_url + "?token="\n'
        'CANONICAL = "https://lakeshore-energy.example/contact"  # allow-literal\n'
    ),
    "web/js/app.mjs": (
        "export async function boot() {\n"
        '  const res = await fetch("/app-config");\n'
        "  const cfg = await res.json();\n"
        '  const proto = location.protocol === "https:" ? "wss:" : "ws:";\n'
        "  const url = `${proto}//${location.host}/ws/handoff/${cfg.id}`;\n"
        "  const hello = {type: 'hello', sampleRate:16000};\n"
        "  const headers = { Authorization: `Bearer ${cfg.token}` };\n"
        "  return { url, hello, headers };\n"
        "}\n"
    ),
    "web/js/scripted-cases.mjs": (
        "export const SCRIPTED_CASES = {\n"
        "  warehouse: { provider: 'Kestrel Wholesale Club' },\n"
        "  bank: { provider: 'Bluefin Harbor Bank' },\n"
        "};\n"
    ),
    "skills/assisted-call/SKILL.md": "---\nname: assisted-call\ndescription: Call a company for the user.\n---\n",
    "company/lakeshore-energy/company.yaml": "company_id: lakeshore-energy\nname: Lakeshore Energy\n",
    "evals/menus/m01.yaml": "menu_id: m01\ngoal: start service\ntext: For new service press 1.\ncorrect_digits: '1'\n",
    "FRICTION-LOG.md": (
        "# Friction log\n\n"
        "## 2026-10-14\n"
        "- pipecat import needed the websocket extra\n\n"
        "## 2026-10-15\n"
        "- tunnel host had to be added to the MCP allow list\n\n"
        "- 2026-10-16 Jev latency over 1.5 s on call_round\n"
        "- 2026-10-17: replay file had to be re-recorded\n"
        "* **2026-10-18** company bot voice changed between runs\n"
        "1. 2026-10-19 final checks rerun after README edits\n"
    ),
}


@dataclass
class FakeRepo:
    root: Path

    def write(self, rel: str, text: str) -> Path:
        path = self.root / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8")
        return path

    def remove(self, rel: str) -> None:
        (self.root / rel).unlink()

    def track(self) -> None:
        subprocess.run(["git", "add", "-A"], cwd=self.root, check=True, capture_output=True, timeout=60)

    def run(self, *args: str) -> subprocess.CompletedProcess[str]:
        # 从仓库外的目录调用：checks.sh 必须自己定位到仓库根目录
        return subprocess.run(
            ["bash", str(self.root / CHECKS_REL), *args],
            cwd=self.root.parent,
            capture_output=True,
            text=True,
            errors="replace",
            timeout=120,
        )


def build_fake_repo(base: Path, checks_sh: Path, *, git: bool = True, files: dict[str, str] | None = None) -> FakeRepo:
    root = base / "repo"
    root.mkdir()
    repo = FakeRepo(root)
    (root / "scripts").mkdir()
    shutil.copyfile(checks_sh, root / CHECKS_REL)
    for rel, text in (CLEAN_FILES if files is None else files).items():
        repo.write(rel, text)
    if git:
        subprocess.run(["git", "init", "-q"], cwd=root, check=True, capture_output=True, timeout=60)
        repo.track()
    return repo


@pytest.fixture
def checks_sh(repo_root: Path) -> Path:
    path = repo_root / CHECKS_REL
    assert path.is_file(), f"{path} is missing"
    return path


@pytest.fixture
def fake_repo(tmp_path: Path, checks_sh: Path) -> FakeRepo:
    return build_fake_repo(tmp_path, checks_sh)


@pytest.fixture
def make_fake_repo(tmp_path: Path, checks_sh: Path) -> Callable[..., FakeRepo]:
    counter = iter(range(1000))

    def make(**kwargs: object) -> FakeRepo:
        base = tmp_path / f"r{next(counter)}"
        base.mkdir()
        return build_fake_repo(base, checks_sh, **kwargs)  # type: ignore[arg-type]

    return make


def output(proc: subprocess.CompletedProcess[str]) -> str:
    return proc.stdout + proc.stderr


def assert_pass(proc: subprocess.CompletedProcess[str]) -> None:
    assert proc.returncode == 0, f"expected exit 0, got {proc.returncode}\n{output(proc)}"


def assert_fail_at(proc: subprocess.CompletedProcess[str], rel: str) -> None:
    assert proc.returncode == 1, f"expected exit 1, got {proc.returncode}\n{output(proc)}"
    assert f"{rel}:" in proc.stdout, f"{rel} not reported\n{output(proc)}"


# ---------------------------------------------------------------------------
# 公共：干净样例、all、用法错误
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("sub", SUBCOMMANDS)
def test_ac17_ac31_ac100_ac102_ac103_ac104_clean_sample_passes(fake_repo: FakeRepo, sub: str) -> None:
    assert_pass(fake_repo.run(sub))


def test_ac17_ac31_ac100_ac102_ac103_ac104_all_passes_on_clean_sample(fake_repo: FakeRepo) -> None:
    assert_pass(fake_repo.run("all"))


def test_ac17_ac31_ac100_ac102_ac103_ac104_all_names_each_failing_check(fake_repo: FakeRepo) -> None:
    fake_repo.write("web/js/promo.mjs", f"export const PARTNER = '{BRAND_WAREHOUSE}';\n")
    fake_repo.write("docs/keys.md", f"id = {FAKE_ACCESS_KEY_ID}\n")
    fake_repo.track()
    proc = fake_repo.run("all")
    assert proc.returncode == 1, output(proc)
    assert "trademarks failed" in proc.stderr and "secrets failed" in proc.stderr
    for ok in ("services", "audio", "hardcoded", "friction"):
        assert f"{ok} failed" not in proc.stderr


@pytest.mark.parametrize(
    "args",
    [(), ("nonsense",), ("friction", "--until", "10/14"), ("friction", "--until"), ("trademarks", "extra")],
)
def test_ac17_ac104_checks_cli_usage_errors_exit_2(fake_repo: FakeRepo, args: tuple[str, ...]) -> None:
    proc = fake_repo.run(*args)
    assert proc.returncode == 2, f"{args}: expected exit 2, got {proc.returncode}\n{output(proc)}"
    assert "usage:" in proc.stderr or "YYYY-MM-DD" in proc.stderr, proc.stderr


# ---------------------------------------------------------------------------
# AC-17 trademarks
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("rel", "text"),
    [
        ("web/js/scripted-cases.mjs", f"export const P = '{BRAND_WAREHOUSE}';\n"),
        ("skills/assisted-call/references/brands.yaml", f"provider: {BRAND_BANK}\n"),
        ("company/lakeshore-energy/www/contact.html", f"<p>CASES.{BRAND_BANK_SHORT.lower()}</p>\n"),
        ("README.md", f"Formerly a {BRAND_WAREHOUSE.upper()} demo.\n"),
    ],
)
def test_ac17_trademarks_flags_brand_in_new_system_paths(fake_repo: FakeRepo, rel: str, text: str) -> None:
    fake_repo.write(rel, text)
    assert_fail_at(fake_repo.run("trademarks"), rel)


def test_ac17_trademarks_ignores_prototype_files_and_vendored_dirs(fake_repo: FakeRepo) -> None:
    # 原型旧文件不在 AC-17 范围（K19、N7）；依赖目录与锁文件也不扫
    fake_repo.write("Waitless-Alexa-MCP-Demo.html", f"<b>{BRAND_WAREHOUSE}</b> {BRAND_BANK}\n")
    fake_repo.write("docs/index.html", f"{BRAND_BANK_SHORT} card\n")
    fake_repo.write("web/node_modules/pkg/index.js", f"// {BRAND_WAREHOUSE}\n")
    fake_repo.write("server/uv.lock", CLEAN_FILES["server/uv.lock"] + f"# {BRAND_WAREHOUSE}\n")
    assert_pass(fake_repo.run("trademarks"))


# ---------------------------------------------------------------------------
# AC-31 services
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "call",
    ['boto3.client("s3")', 'boto3.client("connect", region_name=r)', "boto3.resource('dynamodb')"],
)
def test_ac31_services_flags_other_boto3_service(fake_repo: FakeRepo, call: str) -> None:
    rel = "server/waitless/storage.py"
    fake_repo.write(rel, f"import boto3\n\n\ndef make(r):\n    return {call}\n")
    proc = fake_repo.run("services")
    assert_fail_at(proc, rel)
    assert f"{rel}:5:" in proc.stdout


@pytest.mark.parametrize(
    ("rel", "extra"),
    [
        ("server/pyproject.toml", '  "twilio==9.3.0",\n'),
        ("server/pyproject.toml", '  "google-cloud-storage>=2.18",\n'),
        ("server/uv.lock", '[[package]]\nname = "twilio"\nversion = "9.3.0"\n'),
    ],
)
def test_ac31_services_flags_forbidden_dependency(fake_repo: FakeRepo, rel: str, extra: str) -> None:
    text = CLEAN_FILES[rel]
    if rel.endswith("pyproject.toml"):
        text = text.replace('  "boto3==1.43.112",\n', '  "boto3==1.43.112",\n' + extra, 1)
    else:
        text = text + extra
    fake_repo.write(rel, text)
    assert_fail_at(fake_repo.run("services"), rel)


def test_ac31_services_without_server_project_is_an_error(fake_repo: FakeRepo) -> None:
    assert_pass(fake_repo.run("services"))
    fake_repo.remove("server/pyproject.toml")
    proc = fake_repo.run("services")
    assert proc.returncode == 2, output(proc)
    assert "pyproject.toml" in proc.stderr


# ---------------------------------------------------------------------------
# AC-100 audio
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("rel", ["replays/sample.wav", "server/tests/fixtures/menu.ulaw", "web/assets/beep.mp3"])
def test_ac100_audio_flags_tracked_audio_file(fake_repo: FakeRepo, rel: str) -> None:
    fake_repo.write(rel, "not really audio\n")
    fake_repo.track()
    proc = fake_repo.run("audio")
    assert proc.returncode == 1, output(proc)
    assert rel in proc.stdout


def test_ac100_audio_ignores_untracked_file(fake_repo: FakeRepo) -> None:
    fake_repo.write(".run/local-recording.wav", "local only\n")  # 不 git add：不在仓库里
    assert_pass(fake_repo.run("audio"))


def test_ac100_audio_outside_git_is_an_error(make_fake_repo: Callable[..., FakeRepo]) -> None:
    repo = make_fake_repo(git=False)
    proc = repo.run("audio")
    assert proc.returncode == 2, output(proc)
    assert "git" in proc.stderr


# ---------------------------------------------------------------------------
# AC-102 hardcoded
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("rel", "line"),
    [
        ("server/waitless/app.py", 'BIND = "localhost"\n'),
        ("server/waitless/calls.py", 'CARRIER = "127.0.0.1"\n'),
        ("server/waitless/decision/jev.py", 'URL = "https://api.example.org/v1"\n'),
        ("web/js/handoff-audio.mjs", "const u = 'ws://' + location.host;\n"),
        ("server/waitless/company/app.py", "run(app, port=8080)\n"),
        ("server/waitless/official_number.py", 'MIRROR = "10.20.30.40"\n'),
        ("web/js/mcp-client.mjs", "const target = 'api.example.org:8443';\n"),
    ],
)
def test_ac102_hardcoded_flags_literal(fake_repo: FakeRepo, rel: str, line: str) -> None:
    fake_repo.write(rel, "# header\n" + line)
    proc = fake_repo.run("hardcoded")
    assert_fail_at(proc, rel)
    assert f"{rel}:2:" in proc.stdout


def reported_lines(proc: subprocess.CompletedProcess[str]) -> set[tuple[str, str]]:
    return set(re.findall(r"^([^:\s]+):(\d+):", proc.stdout, re.MULTILINE))


def test_ac102_hardcoded_source_line_refs_pass_but_ports_still_flagged(fake_repo: FakeRepo) -> None:
    """源码注释里的「文件名.py:行号」引用（r2 骨架里就有）不报；同一行或同一文件里真正写死的端口照报。"""
    rel = "server/waitless/call/injector.py"
    fake_repo.write(
        rel,
        '"""依赖私有 _send_text_event（llm.py:943-955）。"""\n'
        "# 旗标先复位（llm.py:1262-1305），见 twilio.py:186-207\n"
        "# USER FINAL 不作为独立管线帧推出（llm.py:1501-1569）\n"
        "uvicorn.run(app, port=8000)  # llm.py:1501-1569\n"
        'TARGET = "media:8443"  # see line_ws.py:12\n',
    )
    js = "web/js/handoff-audio.mjs"
    fake_repo.write(js, "// 参考 handoff.py:1501 与 app.mjs:2048\nconst u = base + ':9000';\n")
    proc = fake_repo.run("hardcoded")
    assert proc.returncode == 1, output(proc)
    assert reported_lines(proc) == {(rel, "4"), (rel, "5"), (js, "2")}, proc.stdout


def test_ac102_hardcoded_scope_is_server_waitless_and_web_js(fake_repo: FakeRepo) -> None:
    literal = 'HOST = "127.0.0.1"; URL = "http://localhost:8080"\n'
    fake_repo.write("server/tests/test_local.py", literal)
    fake_repo.write("scripts/run.sh", literal)
    fake_repo.write("server/config/engine.yaml", "# " + literal)
    fake_repo.write("web/index.html", literal)
    assert_pass(fake_repo.run("hardcoded"))


# ---------------------------------------------------------------------------
# AC-103 secrets
# ---------------------------------------------------------------------------

SECRET_CASES = [
    ("docs/notes.md", f"key id: {FAKE_ACCESS_KEY_ID}\n", FAKE_ACCESS_KEY_ID),
    ("server/waitless/keys.py", f'PEM = """{PRIVATE_KEY_HEAD}\nMIIEow\n"""\n', "MIIEow"),
    ("web/js/api.mjs", f"const h = {{ Authorization: 'Bearer {FAKE_BEARER}' }};\n", FAKE_BEARER),
    (".env.example", f"aws_secret_access_key = {FAKE_SECRET_40}\n", FAKE_SECRET_40),
]


@pytest.mark.parametrize(("rel", "text", "value"), SECRET_CASES, ids=["access-key-id", "private-key", "bearer", "secret-40"])
def test_ac103_secrets_flags_without_printing_value(fake_repo: FakeRepo, rel: str, text: str, value: str) -> None:
    fake_repo.write(rel, text)
    fake_repo.track()
    proc = fake_repo.run("secrets")
    assert_fail_at(proc, rel)
    assert value not in output(proc), "checks.sh secrets must print file:line only, never the value"


def test_ac103_secrets_ignores_untracked_files(fake_repo: FakeRepo) -> None:
    fake_repo.write(".env", f"AWS_ACCESS_KEY_ID={FAKE_ACCESS_KEY_ID}\n")  # 本机 .env 不入库（不 git add）
    assert_pass(fake_repo.run("secrets"))


# ---------------------------------------------------------------------------
# AC-104 friction
# ---------------------------------------------------------------------------


def test_ac104_friction_missing_day_fails_and_names_it(fake_repo: FakeRepo) -> None:
    text = CLEAN_FILES["FRICTION-LOG.md"].replace("- 2026-10-16 Jev latency over 1.5 s on call_round\n", "")
    fake_repo.write("FRICTION-LOG.md", text)
    proc = fake_repo.run("friction")
    assert proc.returncode == 1, output(proc)
    assert "2026-10-16" in proc.stdout
    assert "2026-10-15" not in proc.stdout


def test_ac104_friction_missing_file_fails(fake_repo: FakeRepo) -> None:
    fake_repo.remove("FRICTION-LOG.md")
    proc = fake_repo.run("friction")
    assert proc.returncode == 1, output(proc)
    assert "FRICTION-LOG.md" in output(proc)


def test_ac104_friction_until_checks_only_past_days(fake_repo: FakeRepo) -> None:
    fake_repo.write("FRICTION-LOG.md", "## 2026-10-14\n- first day\n\n- 2026-10-15 second day\n")
    assert_pass(fake_repo.run("friction", "--until", "2026-10-15"))
    proc = fake_repo.run("friction")
    assert proc.returncode == 1, output(proc)
    assert "2026-10-16" in proc.stdout and "2026-10-19" in proc.stdout


def test_ac104_friction_heading_without_entry_does_not_count(fake_repo: FakeRepo) -> None:
    text = CLEAN_FILES["FRICTION-LOG.md"].replace(
        "## 2026-10-15\n- tunnel host had to be added to the MCP allow list\n\n", "## 2026-10-15\n\n"
    )
    fake_repo.write("FRICTION-LOG.md", text)
    proc = fake_repo.run("friction")
    assert proc.returncode == 1, output(proc)
    assert "2026-10-15" in proc.stdout
