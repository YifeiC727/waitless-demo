"""I-3b 补测 · 通话中途回放对不上（第 3 步可见组，冻结）：AC-89（test-gaps G18 的通话层）。

事实（G 线已合并代码）：DecisionGate 把 Jev 客户端抛出的任何异常都改成 JevError（`raise JevError(...) from None`，
原异常仍在 JevError 的 __context__ 里）。所以回放在通话中途对不上时，TextCall 收到的是 JevError。
AC-89 要求「报 ReplayMismatch 并指出通道和序号」：这通电话以失败结束（不挂住、不是 confirmed），trace 的 error 事件
的 action 写成 {"error_type": "ReplayMismatch", "channel": 通道, "seq": 序号}（不含台词），CallSession.wait_ended()
抛出的是原来的 ReplayMismatch（通道、序号与回放文件里缺的那一条一致）。

做法：text-sim 按主线录一份 baseline（录制器包住两个替身，拿到 id 后 bind），把 jev 通道从第一条 said_as_decided
（DP-C2，注入之后的核对）起截掉，再在 replay 模式下按主线跑；conftest 已清掉密钥并拦住一切出站连接。
"""

import json
import sqlite3
from contextlib import closing

import pytest

from e_support import build_settings, replay_header
from e_support.integration import build_stack, run_scenario, scenario_settings
from e_support.replayfiles import read_replay, write_replay
from waitless.interfaces import ReplayMismatch


async def test_ac89_i3b_a_missing_said_check_mid_call_is_reported_as_replay_mismatch(tmp_path, mode_env, engine_config, test_profile, test_account):
    from waitless.replay.recorder import ReplayRecorder

    full = tmp_path / "replays" / "full.jsonl"
    recorders: list = []

    def recording(fake_jev, fake_converse, registry):
        rec = ReplayRecorder(full, replay_header(scenario_id="baseline"), registry=registry)
        recorders.append(rec)
        return rec.wrap_jev(fake_jev), rec.wrap_converse(fake_converse)

    first = build_stack(scenario_settings(build_settings, mode_env, engine_config, tmp_path / "rec"), scenario_id="baseline",
                        profile=test_profile, account=test_account, clients=recording)
    recorded = await run_scenario(first, "baseline", on_ids=lambda t, c: recorders[0].bind(task_id=t, call_id=c))
    recorders[0].close()
    assert recorded.case.outcome == "confirmed"

    header, records, _ = read_replay(full)
    said = [r for r in records if r.channel == "jev" and r.shape.point == "said_as_decided"]
    assert said, "the recorded call checks at least one injection (DP-C2)"
    cut_at = said[0].seq
    kept = [r.model_dump(mode="json", exclude_none=True) for r in records if not (r.channel == "jev" and r.seq >= cut_at)]
    cut_dir = tmp_path / "cut"
    write_replay(cut_dir / "baseline.jsonl", header, kept)

    settings = scenario_settings(build_settings, mode_env, engine_config, tmp_path / "play", mode="replay",
                                 WAITLESS_REPLAY_DIR=str(cut_dir)).model_copy(update={"replay_file": "baseline.jsonl"})
    second = build_stack(settings, scenario_id="baseline", profile=test_profile, account=test_account, clients=lambda *_: (None, None))
    with pytest.raises(ReplayMismatch) as err:
        await run_scenario(second, "baseline", max_fake_ms=120_000)
    assert (err.value.channel, err.value.seq) == ("jev", cut_at)

    with closing(sqlite3.connect(settings.db_path)) as con:
        (call_id,) = con.execute("select call_id from calls").fetchone()
        (outcome,) = con.execute("select outcome from calls").fetchone()
    assert outcome != "confirmed"
    errors = [e.action or {} for e in second.trace.events(call_id=call_id) if e.event_type == "error"]
    assert {"error_type": "ReplayMismatch", "channel": "jev", "seq": cut_at}.items() <= errors[-1].items(), (
        f"the trace's error event names the channel and the sequence number: {errors}"
    )
    assert "Please" not in json.dumps(errors), "no transcript text in the error event"
