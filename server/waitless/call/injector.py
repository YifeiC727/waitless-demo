"""把决定要说的话注入 Sonic（D 线；tech §6.5、S0 V3、S0-r2 V7；AC-71、AC-73；K3）。

公开接口（§7）：SonicTextInjector.inject(text, decision_id)。
依赖 pipecat-ai==1.12.0 的私有 `_send_text_event(text, role=Role.USER, interactive=True)`（llm.py:943-955）。
"""

from typing import Any

from waitless.interfaces import Clock, Trace

__all__ = ["PIPECAT_VERSION_REQUIRED", "PipecatVersionError", "SonicTextInjector"]

PIPECAT_VERSION_REQUIRED = "1.12.0"


class PipecatVersionError(RuntimeError):
    """pipecat.__version__ 不是 1.12.0：私有注入方法可能已变（K3）。"""


class SonicTextInjector:
    """S0-r2 V7 定论：Pipecat 没有文字注入队列。会话续接换流期间（llm._sc.handoff_in_progress 为真）
    的注入由我们排队，等新流就绪后按序发出：
    - 判断就绪要看 llm._stream 换成了新对象且 llm._audio_input_started 为真；
      旗标 handoff_in_progress 会在换流完成前先复位，只看旗标不够（llm.py:1262-1305）。
    - _send_text_event 在 _stream 或 _prompt_name 为空时静默丢弃，不报错（llm.py:953-954），
      所以不能靠它的返回判断是否送达。
    - 队列里的每条只发一次，不丢、不重发（AC-73）。
    """

    def __init__(self, llm: Any, *, trace: Trace, call_id: str, task_id: str, clock: Clock) -> None:
        """构造时检查 pipecat.__version__ == PIPECAT_VERSION_REQUIRED，不符抛 PipecatVersionError。"""
        raise NotImplementedError

    async def inject(self, text: str, decision_id: str, *, template: str | None = None) -> None:
        """发送 models.SONIC_INSTRUCTION_PREFIX + text；写 injection_sent（decision_id、action.text、action.template）（AC-71）。
        action.text 记不带前缀的 text（A-08）；DP-C2 的 decision_summary 也是这段 text（A-02）。
        注入后 OutputGuard 进入 injected；下一段 ASSISTANT FINAL（SonicTap）送 DP-C2 核对（AC-72，由 LiveActionBackend 处理）。"""
        raise NotImplementedError

    @property
    def pending(self) -> int:
        """排队等新流的注入条数。"""
        raise NotImplementedError

    async def flush(self) -> int:
        """新流就绪时按序发出队列，返回发出条数（LiveCall 在每个音频帧或定时检查时调用）。"""
        raise NotImplementedError

    def stream_ready(self) -> bool:
        raise NotImplementedError
