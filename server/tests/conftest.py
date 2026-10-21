"""后台测试的共用设施（第 3 步写，冻结；tech §12）。

- 标记 needs_keys / needs_device / needs_network：默认不跑（pyproject 的 addopts 排除它们；
  即使 addopts 没合并，这里也会在没给 -m 时把它们跳过）。
- 默认测试零密钥、零网络：
  * 每个测试开始前删掉进程环境里的 WAITLESS_*、AWS 凭证与 Jev 密钥，并让 boto3 读不到 ~/.aws；
  * 除回环地址外，任何出站连接和域名解析都抛 NetworkBlocked。
  标了 needs_keys / needs_network 的测试不受对应限制。
- 路径 fixture 指向仓库里的 skills/、company/、web/、evals/menus、replays/、server/config/engine.yaml。
- mode_env(mode)：生成某个模式的完整环境变量字典（测试值，不是密钥），交给 config.load_settings(env)。
  歧义表 A-13（定稿 company_cmd_only_root）：live-sim 组不含 WAITLESS_COMPANY_HOST/PORT；
  测 `waitless company` 时用 mode_env("live-sim", company=True) 加上这两个。
  可选变量（WAITLESS_TEXTSIM_PACE_MS、WAITLESS_RECORD_DIR 等）不在任何组里，需要时作为覆盖值传入。
- 替身 fixture：fake_clock、fake_jev、fake_converse、fake_polly、fake_carrier、fake_trace、make_fake_sonic。
- 测试身份（缺口 B-G-9，定稿 conftest_test_account）：TEST_PROFILE 与 TEST_ACCOUNT 是唯一来源。
  B 的 company/lakeshore-energy/company.yaml 的 customers[0] 照抄这两份（name、phone_number、address 与
  TEST_ACCOUNT["lakeshore-energy"]）；A、X 的测试从这里取。X 的测试按 TEST_PROFILE 的姓名在 customers 里找
  同名客户，不依赖它排第几（缺口 X-G9，定稿 match_by_value，暂定）。

替身从 tests/fakes 导入：`from fakes import FakeJev`（tests 目录本身不是包）。
各线自己的测试放 tests/lane_<线>/，建议放一个空 __init__.py 以免同名测试文件冲突。
"""

import ipaddress
import os
import socket
from collections.abc import Callable, Iterable
from pathlib import Path
from typing import Any

import pytest

SERVER_DIR = Path(__file__).resolve().parents[1]
REPO_ROOT = SERVER_DIR.parent

MARKERS = {
    "needs_keys": "needs real AWS / Jev credentials; run by the I lanes via scripts/load-keys-macos.sh",
    "needs_device": "needs a real phone, browser or a user action",
    "needs_network": "needs outbound network access",
}

SECRET_ENV = (
    "AWS_ACCESS_KEY_ID",
    "AWS_SECRET_ACCESS_KEY",
    "AWS_SESSION_TOKEN",
    "AWS_PROFILE",
    "AWS_DEFAULT_PROFILE",
    "WAITLESS_JEV_API_KEY",
)

# 测试用的诱饵敏感值（AC-42、AC-101）：任何持久化产物或模型输入里都不许出现。
DECOY_SENSITIVE = {
    "ssn": "900-00-0001",
    "date_of_birth": "1900-01-01",
    "card_number": "4000000000000002",
}
# 测试档案的可代说字段（虚构）
TEST_PROFILE = {
    "name": "Alex Example",
    "phone_number": "(312) 555-0147",
    "address": "1200 W Example St, Apt 4B",
}
# 测试档案的公司账户号（虚构；A-04 的 Profile.accounts 形状：company_id → account_number）
TEST_ACCOUNT = {"lakeshore-energy": "100200300"}
# §6.10 Lakeshore Energy 的线路号（555 虚构号段）
TEST_LINE_NUMBER = "(312) 555-0100"


class NetworkBlocked(RuntimeError):
    """默认测试不许出网（tech §12）。"""


# ---------------------------------------------------------------------------
# 标记
# ---------------------------------------------------------------------------


def pytest_configure(config: pytest.Config) -> None:
    for name, text in MARKERS.items():
        config.addinivalue_line("markers", f"{name}: {text}")


def pytest_collection_modifyitems(config: pytest.Config, items: list[pytest.Item]) -> None:
    if config.getoption("markexpr"):
        return  # 显式给了 -m（含 addopts 里的排除式）就按它来
    skip = pytest.mark.skip(reason="excluded from the default run (tech §12); select with -m")
    for item in items:
        if any(item.get_closest_marker(name) for name in MARKERS):
            item.add_marker(skip)


# ---------------------------------------------------------------------------
# 零密钥、零网络
# ---------------------------------------------------------------------------


@pytest.fixture(autouse=True)
def _no_keys(request: pytest.FixtureRequest, monkeypatch: pytest.MonkeyPatch) -> None:
    if request.node.get_closest_marker("needs_keys"):
        return
    for name in list(os.environ):
        if name.startswith("WAITLESS_") or name in SECRET_ENV:
            monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("AWS_SHARED_CREDENTIALS_FILE", os.devnull)
    monkeypatch.setenv("AWS_CONFIG_FILE", os.devnull)
    monkeypatch.setenv("AWS_EC2_METADATA_DISABLED", "true")


def _host_allowed(host: Any) -> bool:
    if host is None:
        return True
    if isinstance(host, bytes):
        host = host.decode("ascii", "ignore")
    host = str(host).strip("[]")
    if host in ("localhost",) or host.endswith(".localhost"):
        return True
    try:
        return ipaddress.ip_address(host.split("%", 1)[0]).is_loopback
    except ValueError:
        return False


@pytest.fixture(autouse=True)
def _no_network(request: pytest.FixtureRequest, monkeypatch: pytest.MonkeyPatch) -> None:
    if request.node.get_closest_marker("needs_network") or request.node.get_closest_marker("needs_keys"):
        return
    real_connect = socket.socket.connect
    real_connect_ex = socket.socket.connect_ex
    real_getaddrinfo = socket.getaddrinfo

    def check(sock: socket.socket, address: Any) -> None:
        if getattr(socket, "AF_UNIX", None) is not None and sock.family == socket.AF_UNIX:
            return
        host = address[0] if isinstance(address, tuple) else address
        if not _host_allowed(host):
            raise NetworkBlocked(f"outbound connection to {address!r} blocked in default tests")

    def guarded_connect(self: socket.socket, address: Any) -> Any:
        check(self, address)
        return real_connect(self, address)

    def guarded_connect_ex(self: socket.socket, address: Any) -> Any:
        check(self, address)
        return real_connect_ex(self, address)

    def guarded_getaddrinfo(host: Any, *args: Any, **kwargs: Any) -> Any:
        if not _host_allowed(host):
            raise NetworkBlocked(f"DNS lookup of {host!r} blocked in default tests")
        return real_getaddrinfo(host, *args, **kwargs)

    monkeypatch.setattr(socket.socket, "connect", guarded_connect)
    monkeypatch.setattr(socket.socket, "connect_ex", guarded_connect_ex)
    monkeypatch.setattr(socket, "getaddrinfo", guarded_getaddrinfo)


# ---------------------------------------------------------------------------
# 路径
# ---------------------------------------------------------------------------


@pytest.fixture(scope="session")
def repo_root() -> Path:
    return REPO_ROOT


@pytest.fixture(scope="session")
def server_dir() -> Path:
    return SERVER_DIR


@pytest.fixture(scope="session")
def skills_dir() -> Path:
    return REPO_ROOT / "skills"


@pytest.fixture(scope="session")
def company_dir() -> Path:
    return REPO_ROOT / "company"


@pytest.fixture(scope="session")
def web_dir() -> Path:
    return REPO_ROOT / "web"


@pytest.fixture(scope="session")
def menus_dir() -> Path:
    return REPO_ROOT / "evals" / "menus"


@pytest.fixture(scope="session")
def replays_dir() -> Path:
    return REPO_ROOT / "replays"


@pytest.fixture(scope="session")
def engine_config_path() -> Path:
    return SERVER_DIR / "config" / "engine.yaml"


@pytest.fixture
def engine_config(engine_config_path: Path) -> Any:
    import yaml

    from waitless.models import EngineConfig

    return EngineConfig.model_validate(yaml.safe_load(engine_config_path.read_text(encoding="utf-8")))


# ---------------------------------------------------------------------------
# 运行模式的环境变量（§5.2；全是测试值，不是密钥）
# ---------------------------------------------------------------------------

MODE_VARS: dict[str, tuple[str, ...]] = {
    "common": (
        "WAITLESS_MODE",
        "WAITLESS_HOST",
        "WAITLESS_PORT",
        "WAITLESS_ALLOWED_HOSTS",
        "WAITLESS_DB_PATH",
        "WAITLESS_WEB_DIR",
        "WAITLESS_SKILLS_DIR",
        "WAITLESS_COMPANY_DIR",
        "WAITLESS_ENGINE_CONFIG",
    ),
    "models": (
        "AWS_REGION",
        "AWS_ACCESS_KEY_ID",
        "AWS_SECRET_ACCESS_KEY",
        "WAITLESS_PLANNER_MODEL_ID",
        "WAITLESS_JEV_URL",
        "WAITLESS_JEV_MODEL",
        "WAITLESS_JEV_API_KEY",
    ),
    "live": (
        "WAITLESS_CARRIER_URL",
        "WAITLESS_LINE_WS_URL",
        "WAITLESS_SONIC_MODEL_ID",
        "WAITLESS_SONIC_VOICE",
    ),
    "replay": ("WAITLESS_REPLAY_DIR",),
    # A-13：只有 `waitless company` 子命令必需（config.REQUIRED_FOR_COMPANY），不属于任何模式的必需项
    "company": ("WAITLESS_COMPANY_HOST", "WAITLESS_COMPANY_PORT"),
}


def build_mode_env(mode: str, db_dir: Path, *, company: bool = False) -> dict[str, str]:
    values = {
        "WAITLESS_MODE": mode,
        "WAITLESS_HOST": "127.0.0.1",
        "WAITLESS_PORT": "0",
        "WAITLESS_ALLOWED_HOSTS": "127.0.0.1,localhost",
        "WAITLESS_DB_PATH": str(db_dir / "waitless.db"),
        "WAITLESS_WEB_DIR": str(REPO_ROOT / "web"),
        "WAITLESS_SKILLS_DIR": str(REPO_ROOT / "skills"),
        "WAITLESS_COMPANY_DIR": str(REPO_ROOT / "company"),
        "WAITLESS_ENGINE_CONFIG": str(SERVER_DIR / "config" / "engine.yaml"),
        "AWS_REGION": "us-east-1",
        "AWS_ACCESS_KEY_ID": "test-not-a-key",
        "AWS_SECRET_ACCESS_KEY": "test-not-a-secret",
        "WAITLESS_PLANNER_MODEL_ID": "test-planner-model",
        "WAITLESS_JEV_URL": "http://jev.invalid/v1/systemone",
        "WAITLESS_JEV_MODEL": "test-jev-model",
        "WAITLESS_JEV_API_KEY": "test-not-a-key",
        "WAITLESS_CARRIER_URL": "http://carrier.invalid",
        "WAITLESS_LINE_WS_URL": "ws://engine.invalid/ws/line",
        "WAITLESS_COMPANY_HOST": "127.0.0.1",
        "WAITLESS_COMPANY_PORT": "0",
        "WAITLESS_SONIC_MODEL_ID": "test-sonic-model",
        "WAITLESS_SONIC_VOICE": "test-voice",
        "WAITLESS_REPLAY_DIR": str(REPO_ROOT / "replays"),
    }
    groups = {
        "live-sim": ("common", "models", "live"),
        "text-sim": ("common", "models"),
        "replay": ("common", "replay"),
    }[mode]
    if company:
        groups = (*groups, "company")
    return {name: values[name] for g in groups for name in MODE_VARS[g]}


@pytest.fixture
def mode_env(tmp_path: Path) -> Callable[..., dict[str, str]]:
    """mode_env("text-sim", drop=["WAITLESS_JEV_URL"], WAITLESS_PORT="1") → 环境变量字典。
    company=True 再加上 `waitless company` 必需的 WAITLESS_COMPANY_HOST/PORT（A-13）。"""

    def make(mode: str, *, drop: Iterable[str] = (), company: bool = False, **overrides: str) -> dict[str, str]:
        env = build_mode_env(mode, tmp_path, company=company)
        for name in drop:
            env.pop(name, None)
        env.update(overrides)
        return env

    return make


# ---------------------------------------------------------------------------
# 替身
# ---------------------------------------------------------------------------


@pytest.fixture
def fake_clock() -> Any:
    from fakes import FakeClock

    return FakeClock()


@pytest.fixture
def fake_jev() -> Any:
    from fakes import FakeJev

    return FakeJev()


@pytest.fixture
def fake_converse() -> Any:
    from fakes import FakeConverse

    return FakeConverse()


@pytest.fixture
def fake_polly() -> Any:
    from fakes import FakePolly

    return FakePolly()


@pytest.fixture
def fake_carrier() -> Any:
    from fakes import FakeCarrier

    return FakeCarrier(numbers={TEST_LINE_NUMBER: TEST_LINE_NUMBER}, scenarios={TEST_LINE_NUMBER: "baseline"})


@pytest.fixture
def fake_trace(fake_clock: Any) -> Any:
    """D、E、G 共用的内存 trace（缺口 G-G-01），t_ms 取 fake_clock。"""
    from fakes import FakeTrace

    return FakeTrace(fake_clock)


@pytest.fixture
def make_fake_sonic(fake_clock: Any) -> Callable[..., Any]:
    from fakes import FakeSonicService

    def make(**kwargs: Any) -> Any:
        kwargs.setdefault("clock", fake_clock)
        return FakeSonicService(**kwargs)

    return make


@pytest.fixture
def decoy_sensitive() -> dict[str, str]:
    return dict(DECOY_SENSITIVE)


@pytest.fixture
def test_profile() -> dict[str, str]:
    return dict(TEST_PROFILE)


@pytest.fixture
def test_account() -> dict[str, str]:
    return dict(TEST_ACCOUNT)
