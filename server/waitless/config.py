"""读取并校验环境变量与 engine.yaml（A 线；tech §5；AC-36）。

公开接口（§7）：load_settings(env) -> Settings、ConfigError(missing)、REQUIRED_BY_MODE。

歧义表 A-27（定稿 split_engine_frozen）：Settings 与 SETTINGS_ENV 定义在本文件，归 A 线所有，A 可以自行增补
环境变量（如 A-20 的 WAITLESS_TEXTSIM_PACE_MS），在报告里通知各线；其他线 `from waitless.config import Settings`。
EngineConfig（engine.yaml 的结构）仍在冻结的 models.py。
下面 Settings、SETTINGS_ENV、REQUIRED_FOR_COMPANY 的结构是第 3 步按 §5.2 与定稿写好的起点，A 线可增不可删
（删字段会打断 D、E 读的 sonic_model_id、record_dir 等）。
"""

from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field, SecretStr

from waitless.models import EngineConfig, Mode

__all__ = [
    "REQUIRED_BY_MODE",
    "REQUIRED_FOR_COMPANY",
    "SETTINGS_ENV",
    "ConfigError",
    "Settings",
    "SystemClock",
    "load_engine_config",
    "load_settings",
]


class Settings(BaseModel):
    """config.load_settings() 的产物（§5.2）。密钥用 SecretStr，打印时不露值。

    按模式必需哪些变量由 REQUIRED_BY_MODE 决定；这里只定结构。
    textsim_pace_ms（A-20）：可选变量 WAITLESS_TEXTSIM_PACE_MS 的原值；有值时 load_settings 同时把
    engine.textsim.pace_ms 换成它（TextCall 只读 engine.textsim.pace_ms）。测试和评测不设，保持文件里的 0。
    planner、codex_command、codex_timeout_s（D14-1）：WAITLESS_PLANNER=bedrock|codex（默认 bedrock），
    codex 只允许 text-sim；codex_command 是 WAITLESS_CODEX_COMMAND 按 shlex 切分的结果。
    replay_file（缺口 X-G5）：可选变量 WAITLESS_REPLAY_FILE，WAITLESS_REPLAY_DIR 下要用的回放文件名，
    没设时用 baseline-web.jsonl（网页演示那份）。
    company_stats_path（缺口 X-G6，定稿 stats_jsonl，暂定）：可选变量 WAITLESS_COMPANY_STATS，模拟公司每通结束时
    往这个文件追加一行 JSON {call_sid, nonsilent_inbound_frames_during_handoff}（AC-98）；没设时不写。
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    mode: Mode
    host: Annotated[str, Field(min_length=1)]
    port: Annotated[int, Field(ge=0, le=65535)]
    allowed_hosts: tuple[str, ...]
    public_base_url: str | None = None
    db_path: Path
    web_dir: Path
    skills_dir: Path
    company_dir: Path
    engine_config_path: Path
    carrier_url: str | None = None
    line_ws_url: str | None = None
    company_host: str | None = None
    company_port: Annotated[int, Field(ge=0, le=65535)] | None = None
    aws_region: str | None = None
    aws_access_key_id: SecretStr | None = None
    aws_secret_access_key: SecretStr | None = None
    sonic_model_id: str | None = None
    sonic_voice: str | None = None
    planner_model_id: str | None = None
    jev_url: str | None = None
    jev_model: str | None = None
    jev_api_key: SecretStr | None = None
    replay_dir: Path | None = None
    record_dir: Path | None = None
    textsim_pace_ms: Annotated[int, Field(ge=0)] | None = None
    planner: Literal["bedrock", "codex"] = "bedrock"
    codex_command: tuple[str, ...] | None = None
    codex_timeout_s: Annotated[float, Field(gt=0)] | None = None
    replay_file: str | None = None
    company_stats_path: Path | None = None
    engine: EngineConfig


# 环境变量名 → Settings 字段（§5.2；A-20 加 WAITLESS_TEXTSIM_PACE_MS）。钥匙串条目名只给 load-keys-macos.sh 用，不进 Settings。
SETTINGS_ENV: dict[str, str] = {
    "WAITLESS_MODE": "mode",
    "WAITLESS_HOST": "host",
    "WAITLESS_PORT": "port",
    "WAITLESS_ALLOWED_HOSTS": "allowed_hosts",
    "WAITLESS_PUBLIC_BASE_URL": "public_base_url",
    "WAITLESS_DB_PATH": "db_path",
    "WAITLESS_WEB_DIR": "web_dir",
    "WAITLESS_SKILLS_DIR": "skills_dir",
    "WAITLESS_COMPANY_DIR": "company_dir",
    "WAITLESS_ENGINE_CONFIG": "engine_config_path",
    "WAITLESS_CARRIER_URL": "carrier_url",
    "WAITLESS_LINE_WS_URL": "line_ws_url",
    "WAITLESS_COMPANY_HOST": "company_host",
    "WAITLESS_COMPANY_PORT": "company_port",
    "AWS_REGION": "aws_region",
    "AWS_ACCESS_KEY_ID": "aws_access_key_id",
    "AWS_SECRET_ACCESS_KEY": "aws_secret_access_key",
    "WAITLESS_SONIC_MODEL_ID": "sonic_model_id",
    "WAITLESS_SONIC_VOICE": "sonic_voice",
    "WAITLESS_PLANNER_MODEL_ID": "planner_model_id",
    "WAITLESS_JEV_URL": "jev_url",
    "WAITLESS_JEV_MODEL": "jev_model",
    "WAITLESS_JEV_API_KEY": "jev_api_key",
    "WAITLESS_REPLAY_DIR": "replay_dir",
    "WAITLESS_RECORD_DIR": "record_dir",
    "WAITLESS_TEXTSIM_PACE_MS": "textsim_pace_ms",
    "WAITLESS_PLANNER": "planner",
    "WAITLESS_CODEX_COMMAND": "codex_command",
    "WAITLESS_CODEX_TIMEOUT_S": "codex_timeout_s",
    "WAITLESS_REPLAY_FILE": "replay_file",
    "WAITLESS_COMPANY_STATS": "company_stats_path",
}


class ConfigError(Exception):
    """缺少必需配置或值不合法。cli 捕获后退出码 2，并在 stderr 列出变量名（AC-36）。"""

    def __init__(self, missing: Sequence[str], message: str | None = None) -> None:
        self.missing = tuple(missing)
        super().__init__(message or ("missing required configuration: " + ", ".join(self.missing)))


# 模式 → 必需的环境变量名。
# todo(A): 按 tech §5.2「必需于」列填写，三个键 live-sim / text-sim / replay 都要有：
#   全部模式：WAITLESS_MODE、WAITLESS_HOST、WAITLESS_PORT、WAITLESS_ALLOWED_HOSTS、WAITLESS_DB_PATH、
#             WAITLESS_WEB_DIR、WAITLESS_SKILLS_DIR、WAITLESS_COMPANY_DIR、WAITLESS_ENGINE_CONFIG
#   live-sim 另需：WAITLESS_CARRIER_URL、WAITLESS_LINE_WS_URL、AWS_REGION、AWS_ACCESS_KEY_ID、
#             AWS_SECRET_ACCESS_KEY、WAITLESS_SONIC_MODEL_ID、WAITLESS_SONIC_VOICE、WAITLESS_PLANNER_MODEL_ID、
#             WAITLESS_JEV_URL、WAITLESS_JEV_MODEL、WAITLESS_JEV_API_KEY
#   text-sim 另需：AWS_REGION、AWS_ACCESS_KEY_ID、AWS_SECRET_ACCESS_KEY、WAITLESS_PLANNER_MODEL_ID、
#             WAITLESS_JEV_URL、WAITLESS_JEV_MODEL、WAITLESS_JEV_API_KEY
#   replay 另需：WAITLESS_REPLAY_DIR；不要求任何 AWS 或 Jev 变量（AC-36、Q6）
#   歧义表 A-13（定稿 company_cmd_only_root，暂定）：live-sim 不含 WAITLESS_COMPANY_HOST/PORT（见 REQUIRED_FOR_COMPANY）。
#   WAITLESS_TEXTSIM_PACE_MS、WAITLESS_RECORD_DIR、WAITLESS_PUBLIC_BASE_URL、WAITLESS_PLANNER、WAITLESS_CODEX_TIMEOUT_S、
#   WAITLESS_REPLAY_FILE、WAITLESS_COMPANY_STATS 是可选变量，哪种模式都不必需。
#   D14-1：text-sim 且 WAITLESS_PLANNER=codex 时，改为要求 WAITLESS_CODEX_COMMAND，不要求 AWS_* 与
#   WAITLESS_PLANNER_MODEL_ID（Jev 变量照旧必需）；live-sim、replay 下 WAITLESS_PLANNER=codex 是 ConfigError。
#   缺口 X-G7（定稿 committed_replay_env，暂定）：仓库入库一份 .env.replay（只含 replay 模式的非密钥值，路径用相对路径），
#   README 只写 `cp .env.replay .env` 与 `scripts/run.sh replay`；run.sh 读它，load_settings 照常校验。
REQUIRED_BY_MODE: dict[str, tuple[str, ...]] = {}

# 歧义表 A-13：只有 `waitless company` 子命令必需这两个变量（模拟公司进程的回环监听地址）。
# company.app.run_company 自己检查：缺任一项就在 stderr 列出变量名并返回退出码 2；`waitless serve` 不检查它们。
# WAITLESS_COMPANY_DIR 指 company/ 根目录（下含 <company_id>/）。
REQUIRED_FOR_COMPANY: tuple[str, ...] = ("WAITLESS_COMPANY_HOST", "WAITLESS_COMPANY_PORT")


def load_settings(env: Mapping[str, str]) -> Settings:
    """从 env（通常是 os.environ）构造 Settings；缺任一必需变量抛 ConfigError(全部缺失项)。"""
    # todo(A): §5.1 WAITLESS_MODE 必填、不设默认值；未知模式也是 ConfigError。
    # todo(A): 按 REQUIRED_BY_MODE[mode] 一次列出全部缺失项（不是遇到第一个就停）。
    # todo(A): 用 SETTINGS_ENV 映射字段；WAITLESS_ALLOWED_HOSTS 逗号分隔转 tuple；端口转 int。
    # todo(A): live-sim / text-sim 缺密钥时报错，不自动退到 replay（§5.1、Q6）。
    # todo(A): engine = load_engine_config(WAITLESS_ENGINE_CONFIG)。不写任何默认端口、主机、路径或 URL。
    # todo(A): WAITLESS_TEXTSIM_PACE_MS 有值（非负整数，否则 ConfigError）时，engine.textsim.pace_ms 换成它，
    #          textsim_pace_ms 记原值（A-20）；没设时两处都保持文件里的值 / None。
    # todo(A): WAITLESS_PLANNER 只认 bedrock、codex；codex 按上面 D14-1 的规则检查；WAITLESS_CODEX_COMMAND 用 shlex.split。
    raise NotImplementedError


def load_engine_config(path: str | Path) -> EngineConfig:
    """读 server/config/engine.yaml 并按 EngineConfig 校验；文件缺失或结构不符抛 ConfigError。"""
    # todo(A): yaml.safe_load + EngineConfig.model_validate；把 ValidationError 包成 ConfigError。
    raise NotImplementedError


class SystemClock:
    """真实时钟（实现 interfaces.Clock）：单调毫秒、ISO 8601 时间、asyncio.sleep。
    一个进程只建一个实例，Trace、TurnDetector、OutputGuard 共用（时间戳同源，A-15、A-08）。"""

    def monotonic_ms(self) -> int:
        # todo(A): time.monotonic_ns() // 1_000_000
        raise NotImplementedError

    def now_iso(self) -> str:
        # todo(A): datetime.now(timezone.utc).isoformat()
        raise NotImplementedError

    async def sleep(self, seconds: float) -> None:
        # todo(A): await asyncio.sleep(seconds)
        raise NotImplementedError
