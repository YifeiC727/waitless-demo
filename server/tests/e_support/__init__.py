"""E 线（文字通话、评测与回放）测试的共用设施（第 3 步写，冻结；施工线不改）。

这里只有测试用的替身与组装函数，不含产品逻辑：
- stubs.py        按 interfaces.py 的 Protocol 写的进程内替身（公司、决策门、trace、待决选项、L3、令牌、档案存储）
                  和一通 text 通话的组装函数 make_rig()。E 线的单元测试只经这些替身与 TextCall 打交道，
                  不依赖 A、B、G 的实现。
- integration.py  把 A、B、G 的真实模块与 FakeJev / FakeConverse 组成 text-sim 整条决策链（AC-85、86、88、101、107），
                  其中 ScenarioOracle 按场景文件的 expect 应答（「Jev 按场景作者的期望作答」）。
- decoys.py       诱饵敏感值的检索（AC-101、AC-107）。

本包顶层不导入 tests/fakes：fakes/__init__ 会尝试导入 Pipecat，而 AC-87 的子进程探针要求整个运行过程不碰 Pipecat。
"""

from e_support.decoys import decoy_forms, find_decoys
from e_support.stubs import (
    FIELD_CUES,
    POINT_QUESTIONS,
    REAL_GUARD_S,
    REQUEST_TEXT,
    SAY_TEMPLATES,
    DecoyProfile,
    FakeHandoffSocket,
    MemoryTrace,
    Rig,
    ScriptedCompany,
    ScriptedGate,
    SimpleClock,
    StubHandoffTokens,
    StubLineTokens,
    StubLlm,
    StubPending,
    StubRegistry,
    StubStore,
    When,
    bare_decision,
    build_settings,
    full_policy,
    is_template_utterance,
    line,
    make_compiled,
    make_rig,
    make_task_card,
    queue_reactions,
    replay_header,
    run_until,
    spec_ask_user,
    spec_end,
    spec_handoff,
    spec_press,
    spec_release,
    spec_say,
    spec_wait,
    with_pace,
)

__all__ = [
    "FIELD_CUES",
    "POINT_QUESTIONS",
    "REAL_GUARD_S",
    "REQUEST_TEXT",
    "SAY_TEMPLATES",
    "DecoyProfile",
    "FakeHandoffSocket",
    "MemoryTrace",
    "Rig",
    "ScriptedCompany",
    "ScriptedGate",
    "SimpleClock",
    "StubHandoffTokens",
    "StubLineTokens",
    "StubLlm",
    "StubPending",
    "StubRegistry",
    "StubStore",
    "When",
    "bare_decision",
    "build_settings",
    "decoy_forms",
    "find_decoys",
    "full_policy",
    "is_template_utterance",
    "line",
    "make_compiled",
    "make_rig",
    "make_task_card",
    "queue_reactions",
    "replay_header",
    "run_until",
    "spec_ask_user",
    "spec_end",
    "spec_handoff",
    "spec_press",
    "spec_release",
    "spec_say",
    "spec_wait",
    "with_pace",
]
