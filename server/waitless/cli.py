"""命令行入口 `waitless`（A 线；tech §5.4、§6.11、§6.12、§12；AC-34、AC-36、AC-92、AC-95–AC-97）。

子命令：
  waitless serve                                   启动后台（读 env；缺必需项退出码 2）
  waitless company                                 启动模拟公司进程（只监听回环；B 线的 company.app）；
                                                   另需 WAITLESS_COMPANY_HOST/PORT（config.REQUIRED_FOR_COMPANY，A-13）
  waitless eval [--mode text-sim|replay|live-sim] [--audio] [--scenario ID] [--record]
  waitless eval compare --base-ref A --head-ref B  倒退时退出码 1（AC-92）
  waitless check live --scenario ID [--report PATH]  需密钥的音频主线检查（AC-95–AC-97）；--report 写 LiveCheckReport 的 JSON；
                                                   自己起、关模拟公司进程（缺口 X-G1），PRD AC-97 的一条命令即可跑
  waitless trace check <call_id>                   trace 不完整时退出码 1（AC-34）
退出码：0 成功；1 检查不通过；2 配置错误（ConfigError）。
"""

from collections.abc import Sequence
from typing import Literal

from pydantic import BaseModel

from waitless.config import Settings

__all__ = ["EXIT_CHECK_FAILED", "EXIT_CONFIG", "EXIT_OK", "LiveCheckReport", "check_live", "main"]

EXIT_OK = 0
EXIT_CHECK_FAILED = 1
EXIT_CONFIG = 2


class LiveCheckReport(BaseModel):
    """`waitless check live` 的报告（AC-95–AC-97；字段名按 PRD v4 AC-97 与 tech v4 §7.1 AC-96）。

    menu_to_dtmf_ms：每条 dtmf_sent 按 decision_id → decision.action.turn_seq → company_turn 算出的差值（A-08）；
    menu_to_dtmf_ms_p50：它们的 p50，≤4000 才算通过（AC-96）；没有按键时为 null。
    identity_check：第一条文字命中 identity 词表的 company_turn 之后，我方第一句 waitless_utterance 含
    "AI assistant"（不分大小写）且不含 "I am the account holder"、"yes, this is" 时为 pass，否则 fail；
    identity_utterance：那一句原文（找不到时为 null，identity_check 为 fail）（AC-97）。
    """

    scenario_id: str
    call_id: str
    outcome: str
    confirmation_number: str | None
    confirmation_matches: bool
    trace_ok: bool
    menu_to_dtmf_ms: list[int]
    menu_to_dtmf_ms_p50: float | None
    identity_check: Literal["pass", "fail"]
    identity_utterance: str | None


def main(argv: Sequence[str] | None = None) -> int:
    """解析子命令并执行，返回退出码（不直接 sys.exit，便于测试）。"""
    # todo(A): argparse 子命令见模块文档；ConfigError → 在 stderr 打印缺失变量名，返回 EXIT_CONFIG（AC-36）。
    # todo(A): serve：load_settings(os.environ) → create_app → uvicorn.run(host=settings.host, port=settings.port)。
    # todo(A): company：交给 waitless.company.app.run_company(settings)（B 线实现；它自己检查
    #          REQUIRED_FOR_COMPANY，缺项返回 EXIT_CONFIG，A-13）。
    # todo(A): eval：waitless.evals.runner.run_eval(...)；--record 时录到 settings.record_dir/<scenario_id>.jsonl，
    #          同名已存在就报错、不覆盖（缺口 X-G4）。
    # todo(A): compare：waitless.evals.compare.compare_refs(base, head, repo_root=, out_dir=, settings=, replays=)，
    #          replays 按 ref 找 replays/eval-<ref>.jsonl（缺口 E-G-2，定稿 replay_per_ref），缺哪个就报错退出；
    #          有倒退返回 EXIT_CHECK_FAILED（AC-92）。
    # todo(A): trace check：trace.Trace.check_call(call_id) 非空 → EXIT_CHECK_FAILED（AC-34；A-24 定为 Trace 的方法）。
    # todo(A): check live：check_live(...) 的报告按 --report 路径写 JSON（AC-97 的 .run/check-baseline.json）；
    #          confirmation_matches、trace_ok、p50 ≤4000、identity_check=pass 全满足才返回 EXIT_OK。
    raise NotImplementedError


async def check_live(scenario: str, settings: Settings) -> LiveCheckReport:
    """跑一通 live-sim 电话并核对：outcome=confirmed、确认号匹配、trace check 通过、
    菜单结束到 dtmf_sent 的 p50 ≤4000 ms、身份那一轮之后的 waitless_utterance 含 "AI assistant"。

    缺口 X-G1（定稿 self_contained，Jev .52）：check live 自己起模拟公司进程——用同一份 env 以子进程跑
    `waitless company`（回环地址，REQUIRED_FOR_COMPANY 两个变量必需），等端口可连后再拨号，结束（含异常）时关掉并回收；
    主应用在本进程内按 settings 组装（build_services、create_app），监听 settings.host/port 供运营商回连 /ws/line。
    缺口 X-G11（lane_internal，A 线自定）：写的是 settings.db_path 指的库，之后 `waitless trace check <call_id>` 能查到。"""
    # todo(A): 先 PUT /sim/scenario（call.live.CarrierClient），再按主线经 MCP 工具跑一通（AC-95）。
    # todo(A): 用 Trace.menu_to_dtmf_ms(call_id) 计算 p50（AC-96，A-08 的回溯口径）；
    #          按 lexicons.yaml 的 identity 词表找那一轮，填 identity_check 与 identity_utterance（AC-97）。
    # 这是〔需密钥〕检查，只由 I-2 经 scripts/load-keys-macos.sh 运行，不进默认测试。
    raise NotImplementedError
