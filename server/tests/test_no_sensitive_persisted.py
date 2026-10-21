"""AC-101：text-sim 全量跑完后，数据库导出和日志里搜不到测试档案的诱饵 SSN、出生日期、卡号（可见组，冻结；tech §7.1 指定的文件）。

第二组（10/16–10/18）。整条链：A、B、G 的真实模块 + FakeJev/FakeConverse（e_support/integration.py），
经 build_services 组装、按主线（prepare_task → start → 结束）用带诱饵值的夹具档案（档案存储读出的是它）把三个场景都跑完，然后
- 导出 SQLite（iterdump 的全部 SQL）并读数据库文件本身及其 -wal、-shm、-journal 的原始字节；
- 收集全部日志记录（caplog，DEBUG 级）与进程的 stdout/stderr（capfd）；
逐一检索诱饵值（原样与只保留数字的写法）。
"""

import logging
import sqlite3

from e_support import build_settings, find_decoys
from e_support.integration import SCENARIO_IDS, build_stack, run_scenario, scenario_settings


def _db_artifacts(db_path) -> list:
    out: list = []
    con = sqlite3.connect(str(db_path))
    try:
        out.append("\n".join(con.iterdump()))
    finally:
        con.close()
    for path in sorted(db_path.parent.glob(db_path.name + "*")):
        if path.is_file():
            out.append(path.read_bytes())
    return out


async def test_ac101_text_sim_full_run_leaves_no_decoy_in_db_or_logs(
    tmp_path, mode_env, engine_config, decoy_sensitive, test_profile, test_account, caplog, capfd
):
    caplog.set_level(logging.DEBUG)
    haystacks: dict[str, list] = {}
    for sid in SCENARIO_IDS:
        settings = scenario_settings(build_settings, mode_env, engine_config, tmp_path / sid)
        stack = build_stack(settings, scenario_id=sid, profile=test_profile, account=test_account, decoys=decoy_sensitive)
        run = await run_scenario(stack, sid)
        assert run.case.outcome != "in_progress", f"{sid} did not finish"
        assert run.events, f"{sid} wrote no trace"
        stack.store.close()
        haystacks[f"{sid} database"] = _db_artifacts(settings.db_path)

    captured = capfd.readouterr()
    haystacks["log records"] = [rec.getMessage() for rec in caplog.records] + [str(rec.args) for rec in caplog.records]
    haystacks["stdout/stderr"] = [captured.out, captured.err]

    for where, hay in haystacks.items():
        if where.endswith("database"):
            assert hay and hay[0].strip(), f"nothing exported from {where}"
        assert find_decoys(hay, decoy_sensitive) == [], f"decoy value found in {where}"
