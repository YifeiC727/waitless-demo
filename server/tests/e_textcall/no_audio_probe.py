"""AC-87 子进程探针（第 3 步写，冻结；不是测试文件，由 test_textsim_no_audio.py 在新解释器里运行）。

用法：python -B no_audio_probe.py <import|run> <config.json>
  config.json：{"env": mode_env("text-sim") 的字典}

探针在导入任何 waitless 模块之前装一个导入拦截器：导入 pipecat、websockets 或 waitless.call.pipeline
（含子模块）一律记下并抛 ImportError。然后
  import：只导入 waitless.call 与 waitless.call.textcall；
  run：再用 e_support 的进程内替身跑一通 text 通话（按键、说话、挂断），不导入 tests/fakes。
最后在 stdout 最后一行打印 JSON：attempted（被拦下的导入）、loaded（sys.modules 里的这些模块）以及通话结果。
"""

import asyncio
import importlib.abc
import json
import sys
from pathlib import Path

BLOCKED = ("pipecat", "websockets", "waitless.call.pipeline")
attempted: list[str] = []


def _blocked(name: str) -> bool:
    return any(name == b or name.startswith(b + ".") for b in BLOCKED)


class _Blocker(importlib.abc.MetaPathFinder):
    def find_spec(self, name, path=None, target=None):  # type: ignore[no-untyped-def]
        if _blocked(name):
            attempted.append(name)
            raise ImportError(f"blocked by the AC-87 probe: {name}")
        return None


ACCOUNT = "40012345"
SAY_TEXT = "No thank you, please skip the survey."


async def _run(env: dict[str, str]) -> dict[str, object]:
    import yaml

    from e_support import (
        SimpleClock,
        When,
        build_settings,
        line,
        make_rig,
        queue_reactions,
        spec_end,
        spec_press,
        spec_say,
    )
    from waitless import models as m

    engine = m.EngineConfig.model_validate(yaml.safe_load(Path(env["WAITLESS_ENGINE_CONFIG"]).read_text(encoding="utf-8")))
    settings = build_settings(env, engine)
    clock = SimpleClock()
    from e_support import ScriptedCompany

    company = ScriptedCompany(
        [line("Thank you for calling Lakeshore Energy. Please enter your account number, followed by the pound key.")],
        queue_reactions(
            When("digits", ACCOUNT + "#", [line("Thanks. Would you like to take part in a short survey?")]),
            When("speech", SAY_TEXT, [line("Okay. Is there anything else I can help with?")]),
        ),
    )
    rig = make_rig(
        settings=settings,
        clock=clock,
        company=company,
        script=[
            ("account number", spec_press(ACCOUNT + "#", intent="press_field:account_number")),
            ("survey", spec_say(SAY_TEXT)),
            ("anything else", spec_end()),
        ],
        allowed_fields={"name": "Alex Example", "account_number": ACCOUNT},
    )
    session = await rig.start()
    for _ in range(5000):
        if session.ended:
            break
        await clock.advance(100)
    return {
        "ended": bool(session.ended),
        "digits": company.digits(),
        "heard": company.heard(),
        "turns": [t.text for t in rig.gate.turns()],
        "line_tokens": len(rig.line_tokens.issued),
        "ended_calls": list(rig.ended_calls),
    }


def main(argv: list[str]) -> int:
    sys.meta_path.insert(0, _Blocker())
    tests_dir = Path(__file__).resolve().parents[1]
    server_dir = tests_dir.parent
    for p in (str(tests_dir), str(server_dir)):
        if p not in sys.path:
            sys.path.insert(0, p)
    mode = argv[1]
    config = json.loads(Path(argv[2]).read_text(encoding="utf-8"))

    import waitless.call  # noqa: F401
    import waitless.call.textcall  # noqa: F401

    result: dict[str, object] = {"mode": mode}
    if mode == "run":
        result.update(asyncio.run(_run(config["env"])))
    result["attempted"] = list(attempted)
    result["loaded"] = sorted(n for n in sys.modules if _blocked(n))
    print(json.dumps(result))
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
