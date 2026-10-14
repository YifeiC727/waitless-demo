"""决策门：组 Jev 请求、调 Jev、过策略、写 trace（G 线；tech §6.5、§6.6；AC-21、AC-27、AC-30、AC-37、AC-38、AC-47、AC-72）。

公开接口（§7）：DecisionGate.decide_round(turn, ctx)、DecisionGate.decide(point, request)、round_candidates(turn, compiled)。
"""

from collections.abc import Callable
import uuid

from waitless.decision.policy import Policy
from waitless.decision.registry import Registry
from waitless.decision.jev import build_state, build_request, JevError
from waitless.decision.menu_parse import MENU_LABEL_MAX
from waitless.interfaces import Clock, JevClient, Trace
from waitless.models import (Candidate, CompanyTurn, CompiledCall, DecisionPoint, DecisionRequest,
                             DecisionResult, RoundContext, JevQuestion, JevExchange, JevResponse, TraceEvent,
                             NONE_OF_THE_ABOVE)

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
        self._jev, self._jev_model = jev, jev_model
        self._registry, self._policy = registry, policy
        self._trace, self._clock = trace, clock
        self._ids = ids or new_decision_id

    async def decide_round(self, turn: CompanyTurn, ctx: RoundContext) -> DecisionResult:
        """DP-C1：一次 Jev 请求问 4 题（menu_choice 的 instructions 按菜单轮/对话轮二选一），
        候选来自 round_candidates；再 Policy.evaluate_round；写 decision 事件后返回（AC-47）。"""
        if turn.call_id != ctx.call_id:
            raise ValueError("company turn does not belong to this call")
        request = DecisionRequest(decision_id=self._ids(), point="call_round", call_id=ctx.call_id,
                                  task_id=ctx.task_id, turn=turn, candidates=round_candidates(turn, ctx.compiled),
                                  context=ctx.context, transcript=ctx.transcript)
        questions = self._questions("call_round", request)
        state = build_state(task_goal=ctx.compiled.task_card.goal,
                            allowed_fields=ctx.compiled.allowed_fields,
                            standing_rules=request.context.standing_rules,
                            prior_choices=request.context.prior_choices, transcript=request.transcript)
        exchange = await self._ask(state, questions)
        outcome = self._policy.evaluate_round(request, exchange.response.answers, ctx.compiled)
        result = DecisionResult(decision_id=request.decision_id, point=request.point, turn_seq=turn.seq,
                                jev=exchange, **outcome.model_dump(exclude={"learn"}))
        self._record(request, result, outcome, ctx.compiled.skill_versions)
        return result

    async def decide(self, point: DecisionPoint, request: DecisionRequest) -> DecisionResult:
        """其余 7 个点：题目取 registry；criteria_from 按来源生成（tools_list、company_skills、company_intents，
        或 request.candidates）；instructions 的占位用 request.template_vars；再 Policy.evaluate_point。"""
        if request.point != point or point == "call_round":
            raise ValueError("use decide_round for call_round, and match the request point")
        questions = self._questions(point, request)
        card = request.context.task_card
        state = build_state(task_goal=card.goal if card else self._registry.default_task_goal,
                            allowed_fields=request.context.allowed_fields,
                            standing_rules=request.context.standing_rules,
                            prior_choices=request.context.prior_choices, transcript=request.transcript)
        exchange = await self._ask(state, questions)
        outcome = self._policy.evaluate_point(point, request, exchange.response.answers)
        result = DecisionResult(decision_id=request.decision_id, point=point, jev=exchange,
                                **outcome.model_dump(exclude={"learn"}))
        self._record(request, result, outcome, None)
        return result

    def _questions(self, point, request):
        questions = {}
        for qid, spec in self._registry.point(point).questions.items():
            instructions = spec.instructions
            if point == "call_round" and qid == "menu_choice":
                instructions = spec.instructions_menu if request.turn.kind == "menu" else spec.instructions_conversation
            instructions = instructions.format(**request.template_vars)
            criteria = spec.criteria
            if spec.criteria_from:
                criteria = {c.id: c.description or c.label for c in request.candidates}
            if spec.extra_criteria:
                criteria = {**(criteria or {}), **spec.extra_criteria}
            questions[qid] = JevQuestion(type=spec.type, instructions=instructions, criteria=criteria)
        return questions

    async def _ask(self, state, questions):
        request = build_request(self._jev_model, state, questions)
        start = self._clock.monotonic_ms()
        try:
            response = JevResponse.model_validate(await self._jev.ask(state, questions))
            if set(response.answers) != set(questions):
                raise ValueError("answer keys differ from question keys")
            for qid, spec in questions.items():
                answer = response.answers[qid]
                if answer.type != spec.type:
                    raise ValueError("answer type differs from question type")
                if spec.type == "choice" and (answer.choice not in spec.criteria
                                               or not set(answer.probabilities) <= set(spec.criteria)):
                    raise ValueError("unknown choice criteria")
        except Exception:
            raise JevError("Jev did not provide a valid answer") from None
        latency = max(0, self._clock.monotonic_ms() - start)
        return JevExchange(request=request, response=response, latency_ms=latency)

    def _record(self, request, result, outcome, versions):
        self._trace.event(TraceEvent(task_id=request.task_id, call_id=request.call_id,
                                    event_type="decision", decision_id=result.decision_id,
                                    decision_point=result.point,
                                    jev_request=result.jev.request.model_dump(mode="json"),
                                    jev_response=result.jev.response.model_dump(mode="json"),
                                    policy_decision=outcome.model_dump(mode="json"),
                                    action={"turn_seq": request.turn.seq} if request.turn else None,
                                    latency_ms=result.jev.latency_ms, skill_versions=versions))


def round_candidates(turn: CompanyTurn, compiled: CompiledCall) -> list[Candidate]:
    """菜单轮：解析出的选项 {digits: 截断到 80 字的标签} + none_of_the_above；
    对话轮：compiled.intents 按 §6.6 应答意图表生成（press_field:<key> 只在档案有该字段且公司 skill 声明可键入时出现；
    answer_precall:<question_id> 只在任务卡有该拨号前决定时出现）（AC-47）。"""
    if turn.kind == "menu":
        candidates = [Candidate(id=o.digits, label=o.label[:MENU_LABEL_MAX], description=o.label[:MENU_LABEL_MAX])
                      for o in turn.menu_options]
        candidates.append(Candidate(id=NONE_OF_THE_ABOVE, label="None of the above",
                                    description="None of these options advances the task goal"))
        return candidates
    candidates = []
    for intent in compiled.intents:
        key = intent["id"]
        if key.startswith("press_field:"):
            field = key.split(":", 1)[1]
            if field not in compiled.allowed_fields or field not in compiled.ivr_map["keypad_fields"]:
                continue
        if key.startswith("answer_precall:"):
            question = key.split(":", 1)[1]
            if not any(d.question_id == question and d.selected is not None for d in compiled.task_card.precall_decisions):
                continue
        candidates.append(Candidate(id=key, label=intent.get("label", key), description=intent["description"]))
    return candidates


def new_decision_id() -> str:
    """全局唯一的 decision_id（uuid4）。"""
    return str(uuid.uuid4())
