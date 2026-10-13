"""组任务卡与任务状态机（A 线；tech §6.1 prepare_task/update_task、§6.6 DP-P1–P3、§8 第 1 步；AC-26–AC-30、AC-32）。

公开接口（§7）：TaskService.prepare(request, provider_hint)、TaskService.update(...)、build_task_card()。
"""

from collections.abc import Mapping

from waitless.interfaces import Clock, DecisionGate, LlmTasks, Store, Trace
from waitless.models import (
    DecisionResult,
    ExtractedRequest,
    OfficialNumberOut,
    PrepareTaskOut,
    Skill,
    TaskCard,
    UpdateTaskFields,
    UpdateTaskOut,
)
from waitless.official_number import Fetcher

__all__ = ["TaskService", "build_task_card"]


class TaskService:
    def __init__(
        self,
        *,
        store: Store,
        trace: Trace,
        gate: DecisionGate,
        llm: LlmTasks,
        skills: Mapping[str, Skill],
        fetcher: Fetcher,
        clock: Clock,
    ) -> None:
        raise NotImplementedError

    async def prepare(self, request: str, provider_hint: str | None = None) -> PrepareTaskOut:
        """prepare_task：L1 抽字段 → DP-P1 选公司 skill → DP-P2 选意图 → 查官方号码 → DP-P3 逐题定默认 → 组卡。"""
        # todo(A): 缺口 A-G-6（定稿 gate_appends）：DP-P1 的 candidates 只放公司 skill，DP-P2 只放公司意图；
        #          none_of_the_above 由 G 的 gate 补，这里不放。
        # todo(A): 缺口 A-G-4（定稿 frozen_models）：公司 skill 的 references/ivr-map.yaml、pre-call-questions.yaml
        #          用 yaml.safe_load 后按 models.IvrMap、models.PrecallQuestionFile 校验再用。
        # todo(A): 缺口 A-G-5（定稿 intent_required_fields）：missing = DP-P2 选中意图的 IvrIntent.required_fields
        #          里档案还没有的字段（account_number 看 Profile.accounts[company_id]，A-04）。
        # todo(A): 缺口 A-G-9：返回 status=ready 时，把这一刻（clock.now_iso()）记进 tasks 行（updated_at 或 A 自加的列），
        #          CallManager.start 写 user_action(start_confirmed) 时取它作 shown_ms。update 返回 ready 时同样记。
        # todo(A): DP-P1 最高项是 none_of_the_above → status=unsupported，task_card=null，之后不能开始通话（AC-27）。
        # todo(A): DP-P2 none → message 里请用户澄清（ask_clarify）。
        # todo(A): 一次列出全部缺失的可代说字段（missing），把 pre-call-questions.yaml 的每题带 default 写进卡（AC-29）。
        # todo(A): DP-P3 只有 user_chose:<id> 且 ≥0.85 才改 selected；ask_user 时 needs_user=true 且不预选（AC-30）。
        # todo(A): 官方号码 verified=false（A-14，定稿 setup_required_no_card）：status=setup_required、task_card=null、
        #          missing=[]，message 说明官方号码未能由两个来源核实；tasks.status=setup_required，之后
        #          start_assisted_call 返回 task_not_ready。PrepareTaskOut 的校验器会拒绝别的组合。
        # todo(A): DP-P1–P3 的 DecisionResult.action 恒为 wait，选择在 intent（A-02）；请求的 transcript 放用户原话
        #          （speaker=user，A-01、A-26），建卡前 context.task_card=null（A-09）。
        # todo(A): skills 字段列出本次用到的 skill 的 name/commit/content_sha256；decisions 列出 P1–P3 的结果。
        raise NotImplementedError

    async def update(
        self,
        task_id: str,
        fields: UpdateTaskFields | None = None,
        decisions: Mapping[str, str] | None = None,
    ) -> UpdateTaskOut:
        """update_task：写可代说字段与拨号前选择；card_version 加一。

        歧义表 A-19（定稿 three_values_card_nullable）：返回的 status 取 prepare_task 的 3 个值，task_card 可空
        （unsupported、号码未核实时为 null）；任务 in_call、completed、needs_follow_up、failed 时一律 task_locked。
        """
        # todo(A): 未知 task → ToolFailure("unknown_task")；通话中或已结束 → ToolFailure("task_locked")。
        # todo(A): fields.model_extra 里的任何键（forbidden 或其他未知键）→ ToolFailure("forbidden_field")，
        #          经 Store 的 ForbiddenField（AC-32、A-05）；什么都不写。
        # todo(A): fields.account_number → Store.save_profile({...}, company_id=任务的 company_id)，即
        #          Profile.accounts[company_id]（A-04）；其余可代说字段照常合并进 profile。
        # todo(A): decisions 的 option_id 不在该题选项里 → ToolFailure("invalid_option")。
        raise NotImplementedError

    def get_card(self, task_id: str) -> TaskCard | None:
        raise NotImplementedError


def build_task_card(
    *,
    extracted: ExtractedRequest,
    company_skill: Skill,
    intent: DecisionResult,
    official: OfficialNumberOut,
    profile_fields: Mapping[str, str],
    precall_choices: Mapping[str, DecisionResult],
    card_version: int,
) -> TaskCard:
    """按公司 skill 的 ivr-map.yaml 意图模板生成 goal 与 task_summary，按 pre-call-questions.yaml 生成拨号前问题（AC-28、AC-29）。
    两个文件按 models.IvrMap、models.PrecallQuestionFile 读（A-G-4）；goal、task_summary 用 str.format(**extracted 的字段)
    填（IvrIntent 已校验占位只能是 ExtractedRequest 的字段）。"""
    # todo(A): sources 取 official.sources 中 verified 的官方来源（AC-07 网页渲染成链接）。
    # todo(A): boundary 写明这通电话只办这件事。
    raise NotImplementedError

