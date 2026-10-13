"""挂断后的结果流程（A 线；tech §6.6 DP-E1、DP-E2、§6.7 L4、L5、§8 第 12 步；AC-25、AC-37、AC-38）。

公开接口（§7）：finalize(call_id) -> CaseResult、case_result(task_id)。
歧义表 A-24（定稿 methods_callresult）：§7 的 CaseResult 落成 models.CallResult（一通电话的结论），
case_result(task_id) 再把该任务全部 CallResult 合并成 models.CaseResultOut。
"""

from collections.abc import Mapping

from waitless.interfaces import Clock, DecisionGate, LlmTasks, Store, Trace
from waitless.models import CallResult, CaseResultOut, Skill

__all__ = ["ResultService"]


class ResultService:
    def __init__(
        self,
        *,
        store: Store,
        trace: Trace,
        gate: DecisionGate,
        llm: LlmTasks,
        skills: Mapping[str, Skill],
        clock: Clock,
    ) -> None:
        """缺口 A-G-3（定稿 skills_and_clock）：skills 用来按任务的 company_id 找公司 skill
        （metadata 的 company-id，models.SKILL_COMPANY_ID_KEY），读 references/ivr-map.yaml（按 models.IvrMap 校验）
        的 confirmation_pattern；clock 给 ended_at = clock.now_iso()，duration_s 按 calls.started_at 与 clock 算。"""
        raise NotImplementedError

    async def finalize(self, call_id: str) -> CallResult:
        """L4 抽确认号与陈述 → DP-E1 判结果 → 每条陈述问 DP-E2 → L5 写摘要 → 写 calls 行，返回 CallResult。"""
        # todo(A): confirmed 需同时满足：L4 抽出的确认号匹配公司 skill 的 confirmation_pattern（如 ^LE-\d{5}$），
        #          且 DP-E1 以 ≥0.85 选 confirmed；failed 需 ≥0.85；其余都是 needs_follow_up（AC-37）。
        # todo(A): DP-E1、DP-E2 的 DecisionResult.action 恒为 wait，类别在 intent（A-02）；
        #          DecisionRequest.transcript 放整通转写的要点或待分类的陈述（A-01），call_id 照填。
        # todo(A): DP-E2：verified 与 drop 都需 ≥0.85，否则归 follow_ups（AC-38）。
        # todo(A): verified 每项带 evidence{call_id=本通, turn_seq}（CallResult 校验证据必须指向本通）；
        #          转写来自 trace，转接期间没有转写。
        # todo(A): budget_exhausted 或 say_mismatch 之后挂断的通话没有匹配的确认号，自然落到 needs_follow_up（AC-51、A-02）。
        raise NotImplementedError

    def case_result(self, task_id: str) -> CaseResultOut:
        """合并该任务全部通话：calls 每通一条，history 含各通事件（AC-25）。进行中时 outcome=in_progress，
        进行中那一通的 ended_at、duration_s 为 null（A-10）。"""
        # todo(A): 未知 task → ToolFailure("unknown_task")。
        raise NotImplementedError
