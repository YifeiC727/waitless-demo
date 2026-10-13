"""策略：硬规则、阈值、打扰预算、常设规则与偏好（G 线；tech §6.6；AC-48–AC-58、AC-60；K12）。

公开接口（§7）：Policy.load()、Policy.evaluate_round()、Policy.evaluate_point()。
策略只看 Jev 的结果和程序可核对的事实（词表命中、档案值、任务卡），不看大模型的文字（H3）。
"""

from collections.abc import Mapping
from typing import Any

from pydantic import BaseModel, ConfigDict

from waitless.models import Action, CompiledCall, DecisionPoint, DecisionRequest, JevAnswer, JevCallPhase

__all__ = ["GlobalPolicy", "Policy", "PolicyError", "PolicyOutcome", "ROUND_ORDER"]

# §6.6 call_round 求值顺序
ROUND_ORDER: tuple[str, ...] = (
    "identity",
    "forbidden_info",
    "menu",
    "consequential",
    "standing_rules",
    "preferences",
    "confidence",
)


class PolicyError(Exception):
    """policy.yaml 缺字段或值不合法（AC-48 要求加载即报错）。"""


class GlobalPolicy(BaseModel):
    """decide-option/policy.yaml 的 global 段（键名保留原方案，AC-48）。全部必填，不设默认值。"""

    model_config = ConfigDict(extra="forbid")

    # todo(G): 子结构按 §6.6：interrupt_budget:int；auto_answer{min_confidence}；
    #          ask_user{only_if{min_stakes,max_confidence}, max_options, timeout_sec, on_timeout}；
    #          handoff_always:[str]；never_claim_to_be_user:bool；learn_from_answers{enabled, max_stakes}。
    interrupt_budget: int
    auto_answer: dict[str, Any]
    ask_user: dict[str, Any]
    handoff_always: list[str]
    never_claim_to_be_user: bool
    learn_from_answers: dict[str, Any]


class PolicyOutcome(BaseModel):
    """策略对一次决策的结论；DecisionGate 再配上 decision_id 与 jev 交换，组成 DecisionResult。"""

    intent: str
    action: Action
    digits: str | None = None
    say_text: str | None = None
    call_phase: JevCallPhase | None = None
    reason_code: str
    policy_rule: str
    learn: bool = False  # 用户作答后是否写 preferences（stakes ≤1，AC-58）


class Policy:
    @classmethod
    def load(cls, raw: Mapping[str, Any]) -> "Policy":
        """raw 是合并后的 policy.yaml（version、global、decision_points），即 registry.policy_raw()（缺口 G-G-02）。
        缺字段抛 PolicyError（AC-48）。"""
        raise NotImplementedError

    @property
    def global_(self) -> GlobalPolicy:
        raise NotImplementedError

    def evaluate_round(
        self,
        request: DecisionRequest,
        answers: Mapping[str, JevAnswer],
        compiled: CompiledCall,
    ) -> PolicyOutcome:
        """DP-C1 按 ROUND_ORDER 求值（§6.6 七步）：
        1 identity：命中 identity 词表 → state_ai_identity，即使 Jev 选了别的（AC-60）
        2 forbidden_info：命中 handoff_always 词表，或 Jev 选 request_handoff 且 needs_user ≥0.5：
          先查预算（用完 → budget_exhausted，AC-51）；call_phase 是 live_rep/identity_check → request_handoff，
          否则 ask_for_representative；话术不含任何字段值（AC-52）
        3 menu：menu_choice ≥0.85 记 auto_high_conf，否则取最高项记 low_stakes_top（AC-49）；
          最高项 none_of_the_above → menu_safe_default；动作 press
        4 consequential：round(stakes) ≥2、命中 consequential 词表或 needs_user ≥0.5 即算；任务卡有对应拨号前决定
          → answer_precall（AC-54）；否则预算还有 → ask_user（AC-55，不看 Jev 置信度）；否则 budget_exhausted
        5 standing_rules：survey/marketing → decline_offer（AC-56）
        6 preferences：prior_choices 里 source=preference、question_key 等于本轮命中的 lexicons 标签的
          低后果选择 → 照它（AC-58；PriorChoice 的 question_key 取法见 models.PriorChoice，A-03）。
          缺口 G-G-05（定稿 use_preference_pending_answer）：结果 intent="use_preference:<question_key>"
          （models.USE_PREFERENCE_PREFIX）、action=say、say_text = say_templates["pending.answer"]
          .format(option_label=<偏好的 choice，即 label 原文>, option_detail="")；paperless、language 在第 5 步已由
          常设规则回答，AC-58 用例改用常设规则之外的低后果话题（如回电时段），标签由 G 加进 lexicons
        7 confidence：按 conversation 阈值取 Jev 选择，低于 0.85 取最高项；press_field 只在数字等于档案值时放行，
          否则 ask_repeat（AC-53）；可代说字段的问题不走 forbidden_info（AC-57）；confirm_and_end 要求转写里已有
          符合 confirmation_pattern 的号码或 call_phase=ended，否则先 ask_repeat。"""
        raise NotImplementedError

    def evaluate_point(
        self,
        point: DecisionPoint,
        request: DecisionRequest,
        answers: Mapping[str, JevAnswer],
    ) -> PolicyOutcome:
        """其余 7 个点：min_confidence 与 below_threshold（top_choice/keep_default/needs_follow_up/follow_up），
        以及 rules（如 assisted-call 的 never_auto：4 个用户动作工具 → present_task_card，AC-23）。

        歧义表 A-02（定稿 wait_except_c2）：
        - models.WAIT_ONLY_POINTS：action 固定 wait，选择放 intent。
        - said_as_decided：said ≥ min_probability（0.7）→ intent=said_ok、action=wait；
          否则 request.attempt < retries（1）→ intent=reinject、action=say、say_text=
          request.template_vars[models.SAID_SUMMARY_VAR]；否则 intent=safe_default、reason_code=say_mismatch，
          动作取 policy.yaml 新增的 said_as_decided.safe_default（G 写，建议 end；这通结果落到 needs_follow_up）。
          DecisionResult 的校验器会拒绝别的组合。"""
        raise NotImplementedError

    def should_learn(self, stakes: int) -> bool:
        """learn_from_answers.enabled 且 stakes ≤ max_stakes（1）。学到的 Preference.choice 存选项的 label 原文（G-G-05）。"""
        raise NotImplementedError
