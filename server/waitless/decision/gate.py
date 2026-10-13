"""决策门：组 Jev 请求、调 Jev、过策略、写 trace（G 线；tech §6.5、§6.6；AC-21、AC-27、AC-30、AC-37、AC-38、AC-47、AC-72）。

公开接口（§7）：DecisionGate.decide_round(turn, ctx)、DecisionGate.decide(point, request)、round_candidates(turn, compiled)。
"""

from collections.abc import Callable

from waitless.decision.policy import Policy
from waitless.decision.registry import Registry
from waitless.interfaces import Clock, JevClient, Trace
from waitless.models import Candidate, CompanyTurn, CompiledCall, DecisionPoint, DecisionRequest, DecisionResult, RoundContext

__all__ = ["DecisionGate", "new_decision_id", "round_candidates"]


class DecisionGate:
    """实现 interfaces.DecisionGate。一个实例服务全部决策点；每通电话的数据经 RoundContext/DecisionRequest 传入。"""

    def __init__(
        self,
        *,
        jev: JevClient,
        jev_model: str,
        registry: Registry,
        policy: Policy,
        trace: Trace,
        clock: Clock,
        ids: Callable[[], str] | None = None,
    ) -> None:
        raise NotImplementedError

    async def decide_round(self, turn: CompanyTurn, ctx: RoundContext) -> DecisionResult:
        """DP-C1：一次 Jev 请求问 4 题（menu_choice 的 instructions 按菜单轮/对话轮二选一），
        候选来自 round_candidates；再 Policy.evaluate_round；写 decision 事件后返回（AC-47）。"""
        # todo(G): state = decision.jev.build_state(...)：task_goal、user_profile、standing_rules、prior_choices、
        #          transcript（ctx.transcript 最后 8 行）五项（AC-47）；instructions 只由 skill 文本组成（AC-59）。
        #          白名单与截断只在这里做，调用方不预处理（A-01）。task_goal = ctx.compiled.task_card.goal（A-26）。
        # todo(G): 调 jev.ask(state, questions)，不传决策点（A-06：录制器用 registry.point_of 反推）。
        # todo(G): decision 事件的 action = {"turn_seq": turn.seq}（A-08，menu_to_dtmf_ms 靠它回溯）。
        # todo(G): 缺口 D-G-2（定稿 result_turn_seq）：返回的 DecisionResult.turn_seq = turn.seq（与 action.turn_seq 同值）；
        #          decide() 的其他决策点不填（模型会拒绝）。
        raise NotImplementedError

    async def decide(self, point: DecisionPoint, request: DecisionRequest) -> DecisionResult:
        """其余 7 个点：题目取 registry；criteria_from 按来源生成（tools_list、company_skills、company_intents，
        或 request.candidates）；instructions 的占位用 request.template_vars；再 Policy.evaluate_point。"""
        # todo(G): assistant_next_action 的 criteria = request.candidates（工具名 → description）+ extra_criteria（AC-21）。
        # todo(G): 缺口 A-G-6（定稿 gate_appends）：调用方的 candidates 只有领域候选；这里按 questions.yaml 补上
        #          extra_criteria（DP-A1 的 4 个固定动作）与 none_of_the_above（DP-P1、DP-P2），不要求调用方放。
        # todo(G): said_as_decided 的 {decision_summary} 取 request.template_vars[models.SAID_SUMMARY_VAR]（执行器放的
        #          本次注入文字，AC-72）；transcript 最后一行是刚说的话。
        # todo(G): state 同样由 build_state 组装（A-01）：transcript 取 request.transcript 最后 8 行；
        #          task_goal = context.task_card.goal，task_card 为 null 时用 registry.default_task_goal（A-26）。
        # todo(G): 结果按 A-02：WAIT_ONLY_POINTS → action=wait、选择放 intent；said_as_decided 按 request.attempt
        #          给 said_ok / reinject（say_text=decision_summary）/ safe_default（reason_code=say_mismatch）。
        # todo(G): 调 jev.ask(state, questions)，不传决策点（A-06）。
        # todo(G): 返回前写 trace 的 decision 事件（含 jev_request、jev_response、policy_decision、latency_ms；action 为 null）。
        raise NotImplementedError


def round_candidates(turn: CompanyTurn, compiled: CompiledCall) -> list[Candidate]:
    """菜单轮：解析出的选项 {digits: 截断到 80 字的标签} + none_of_the_above；
    对话轮：compiled.intents 按 §6.6 应答意图表生成（press_field:<key> 只在档案有该字段且公司 skill 声明可键入时出现；
    answer_precall:<question_id> 只在任务卡有该拨号前决定时出现）（AC-47）。"""
    raise NotImplementedError


def new_decision_id() -> str:
    """全局唯一的 decision_id（uuid4）。"""
    raise NotImplementedError
