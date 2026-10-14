"""AC-73：会话续接换流期间，注入器先排队，新流就绪后再发，不丢也不重发。

S0-r2 V7 定论：Pipecat 没有文字注入队列；换流期间（llm._sc.handoff_in_progress 为真）的注入由注入器排队；
判断新流就绪要看 llm._stream 换成了新对象且 llm._audio_input_started 为真（llm.py:1262-1305）。
换流成功时，session_continuation.execute_handoff() 的 finally 复位旗标后立即 return，调用方 _run_sc_handoff()
同步换掉 _stream，中间没有 await：别的协程看不到「旗标已复位、流还是旧的」这个成功中间态，所以成功路径用
complete_continuation() 一步模拟。若看到旗标复位而 _stream 没换，说明换流已放弃（execute_handoff 返回 None），
排队的注入要在原来的流上按序各发一次（第 6 步反审 D-R5 / 修复 D-F3；Jev 裁决 O-G6-D-F3-tests）。
换流窗口里直接发出的文字记为 lost（发到旧流）。
LiveCall 在每个音频帧或定时检查时调 flush()；这里直接调。
"""

from typing import Any

from call_engine._support import injection_texts, make_result, record_decision
from waitless import models as m


def make_injector(sonic: Any, trace: Any, spec: Any, clock: Any) -> Any:
    from waitless.call.injector import SonicTextInjector

    return SonicTextInjector(sonic, trace=trace, call_id=spec.call_id, task_id=spec.task_id, clock=clock)


def decision(trace: Any, spec: Any, text: str, seq: int) -> Any:
    return record_decision(trace, make_result("say", say_text=text, intent="answer_precall:x"),
                           task_id=spec.task_id, call_id=spec.call_id, turn_seq=seq)


async def test_ac73_injection_during_stream_swap_waits_for_the_new_stream(
    make_fake_sonic: Any, trace: Any, spec: Any, fake_clock: Any
) -> None:
    sonic = make_fake_sonic()
    injector = make_injector(sonic, trace, spec, fake_clock)
    d = decision(trace, spec, "Please keep the deposit option.", 1)
    sonic.begin_continuation()
    await injector.inject(d.say_text, d.decision_id)
    assert injector.pending == 1
    assert sonic.injections == [], "text was sent into the stream that is being replaced"
    assert injector.stream_ready() is False

    assert await injector.flush() == 0  # 旗标还立着：继续排队
    assert injector.pending == 1 and sonic.injections == []

    sonic.complete_continuation()  # 换流成功：旗标复位与换新流同步一步完成，_audio_input_started=True
    assert injector.stream_ready() is True
    assert await injector.flush() == 1
    assert injector.pending == 0
    assert [(i.text, i.status, i.generation) for i in sonic.injections] == [
        (m.SONIC_INSTRUCTION_PREFIX + d.say_text, "sent", 2)
    ]
    assert await injector.flush() == 0  # 不重发
    assert len(sonic.injections) == 1



async def test_ac73_abandoned_continuation_sends_queued_text_on_the_old_stream_once(
    make_fake_sonic: Any, trace: Any, spec: Any, fake_clock: Any
) -> None:
    sonic = make_fake_sonic()
    injector = make_injector(sonic, trace, spec, fake_clock)
    d = decision(trace, spec, "Please keep the deposit option.", 1)
    sonic.begin_continuation()
    await injector.inject(d.say_text, d.decision_id)
    assert await injector.flush() == 0 and sonic.injections == []
    sonic.clear_handoff_flag()  # 换流放弃：旗标复位，_stream 没换（execute_handoff 返回 None）
    assert await injector.flush() == 1  # 不丢：在原来的流上发出
    assert injector.pending == 0
    assert [(i.text, i.status, i.generation) for i in sonic.injections] == [
        (m.SONIC_INSTRUCTION_PREFIX + d.say_text, "sent", 1)
    ]
    for _ in range(3):
        assert await injector.flush() == 0  # 不重发
    assert len(sonic.injections) == 1

async def test_ac73_queued_injections_go_out_once_each_in_order(
    make_fake_sonic: Any, trace: Any, spec: Any, fake_clock: Any
) -> None:
    sonic = make_fake_sonic()
    injector = make_injector(sonic, trace, spec, fake_clock)
    before = decision(trace, spec, "Before the swap.", 1)
    await injector.inject(before.say_text, before.decision_id)  # 没在换流：直接发
    sonic.begin_continuation()
    queued = [decision(trace, spec, f"During the swap {i}.", 2 + i) for i in range(3)]
    for d in queued:
        await injector.inject(d.say_text, d.decision_id)
    assert injector.pending == 3
    sonic.complete_continuation()
    assert await injector.flush() == 3
    await injector.flush()
    assert [i.status for i in sonic.injections] == ["sent"] * 4  # 没有 lost、没有 dropped
    assert sonic.sent_texts == [m.SONIC_INSTRUCTION_PREFIX + t for t in ["Before the swap."] + [d.say_text for d in queued]]
    assert [i.generation for i in sonic.injections] == [1, 2, 2, 2]
    # trace 每次注入一条 injection_sent（排队后发出也不重复记）
    assert injection_texts(trace, spec.call_id) == ["Before the swap."] + [d.say_text for d in queued]
    assert [e.decision_id for e in trace.of_type("injection_sent", call_id=spec.call_id)] == [
        before.decision_id, *[d.decision_id for d in queued]
    ]
