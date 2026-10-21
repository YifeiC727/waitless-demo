"""策略：硬规则、阈值、打扰预算、常设规则与偏好（G 线；tech §6.6；AC-48–AC-58、AC-60；K12）。

公开接口（§7）：Policy.load()、Policy.evaluate_round()、Policy.evaluate_point()。
策略只看 Jev 的结果和程序可核对的事实（词表命中、档案值、任务卡），不看大模型的文字（H3）。
"""

from collections.abc import Mapping, Sequence
from typing import Any
import copy
import json
import re

from pydantic import BaseModel, ConfigDict, Field, ValidationError, model_validator

from waitless.models import (Action, CompiledCall, DecisionPoint, DecisionRequest, JevAnswer, JevCallPhase,
                             SAID_SUMMARY_VAR, NONE_OF_THE_ABOVE, FIELD_CUES_PREFIX, USER_ACTION_TOOLS)

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


def _keypad_digits(key: str, value: str | None) -> str | None:
    if not value:
        return None
    if key == "phone_number":
        digits = re.sub(r"\D", "", value)
        if len(digits) == 11 and digits.startswith("1"):
            digits = digits[1:]
        return digits or None
    return value if re.fullmatch(r"[0-9]+", value) else None


class PolicyError(Exception):
    """policy.yaml 缺字段或值不合法（AC-48 要求加载即报错）。"""


class _AutoAnswer(BaseModel):
    model_config = ConfigDict(extra="forbid")
    min_confidence: float = Field(ge=0, le=1)


class _OnlyIf(BaseModel):
    model_config = ConfigDict(extra="forbid")
    min_stakes: int = Field(ge=0, le=3)
    max_confidence: float = Field(ge=0, le=1)


class _AskUser(BaseModel):
    model_config = ConfigDict(extra="forbid")
    only_if: _OnlyIf
    max_options: int = Field(ge=1, le=3)
    timeout_sec: float = Field(gt=0)
    on_timeout: str


class _Learn(BaseModel):
    model_config = ConfigDict(extra="forbid")
    enabled: bool
    max_stakes: int = Field(ge=0, le=1)


class GlobalPolicy(BaseModel):
    """decide-option/policy.yaml 的 global 段（键名保留原方案，AC-48）。全部必填，不设默认值。"""

    model_config = ConfigDict(extra="forbid")

    interrupt_budget: int = Field(ge=0)
    repeat_hold_ms: int = Field(strict=True, ge=1, le=20000)
    auto_answer: dict[str, Any]
    ask_user: dict[str, Any]
    handoff_always: list[str]
    never_claim_to_be_user: bool
    learn_from_answers: dict[str, Any]

    @model_validator(mode="after")
    def _nested(self):
        self.auto_answer = _AutoAnswer.model_validate(self.auto_answer).model_dump()
        self.ask_user = _AskUser.model_validate(self.ask_user).model_dump()
        self.learn_from_answers = _Learn.model_validate(self.learn_from_answers).model_dump()
        return self


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
        if raw.get("version") != 1:
            raise PolicyError("unsupported policy version")
        policy = cls()
        try:
            policy._global = GlobalPolicy.model_validate(raw.get("global", {}))
        except ValidationError as exc:
            raise PolicyError("invalid global policy") from exc
        points = raw.get("decision_points")
        if not isinstance(points, Mapping):
            raise PolicyError("decision_points must be a mapping")
        from waitless.models import DECISION_POINT_CODES
        if not set(DECISION_POINT_CODES) <= points.keys():
            raise PolicyError("missing decision point policies")
        policy._points = copy.deepcopy(dict(points))
        for point, config in policy._points.items():
            if (not isinstance(config, Mapping) or not isinstance(config.get("actions"), Mapping)
                    or not config["actions"]):
                raise PolicyError(f"missing actions for {point}")
            threshold = config.get("min_probability" if point == "said_as_decided" else "min_confidence")
            if not isinstance(threshold, (int, float)) or isinstance(threshold, bool) or not 0 <= threshold <= 1:
                raise PolicyError(f"invalid threshold for {point}")
        round_ = policy._points["call_round"]
        order = round_.get("order")
        if not isinstance(order, (list, tuple)) or tuple(order) != ROUND_ORDER:
            raise PolicyError("invalid call_round order")
        for name in ("menu", "conversation", "consequential", "safe_defaults"):
            if not isinstance(round_.get(name), Mapping):
                raise PolicyError(f"missing call_round {name}")
        for name in ("menu", "conversation"):
            threshold = round_[name].get("min_confidence")
            if (not isinstance(threshold, (int, float)) or isinstance(threshold, bool)
                    or not 0 <= threshold <= 1 or round_[name].get("below_threshold") != "top_choice"):
                raise PolicyError(f"invalid call_round {name} threshold")
        consequential = round_["consequential"]
        min_stakes, needs = consequential.get("min_stakes"), consequential.get("needs_user_threshold")
        if (not isinstance(min_stakes, int) or isinstance(min_stakes, bool) or not 0 <= min_stakes <= 3
                or not isinstance(needs, (float, int)) or isinstance(needs, bool) or not 0 <= needs <= 1):
            raise PolicyError("invalid consequential thresholds")
        defaults = round_["safe_defaults"]
        if any(not isinstance(defaults.get(key), str) or not defaults[key]
               for key in ("menu_none", "ask_user_timeout", "budget_exhausted", "handoff_not_live_rep",
                           "press_field_refused", "end_without_confirmation")):
            raise PolicyError("missing call_round safe defaults")
        said = policy._points["said_as_decided"]
        if not isinstance(said.get("retries"), int) or isinstance(said["retries"], bool) or said["retries"] < 0:
            raise PolicyError("said_as_decided requires nonnegative retries")
        safe_default = said.get("safe_default")
        if not isinstance(safe_default, str) or safe_default not in {"end", "wait", "handoff", "ask_user", "release"}:
            raise PolicyError("said_as_decided needs an executable safe_default")
        return policy

    @property
    def global_(self) -> GlobalPolicy:
        return self._global.model_copy(deep=True)

    def evaluate_round(
        self,
        request: DecisionRequest,
        answers: Mapping[str, JevAnswer],
        compiled: CompiledCall,
    ) -> PolicyOutcome:
        """DP-C1 按 ROUND_ORDER 求值（§6.6 七步）：
        1 identity：命中 identity 词表 → state_ai_identity，即使 Jev 选了别的（AC-60）
        2 forbidden_info：命中 handoff_always 词表，或 Jev 选 request_handoff 且 needs_user ≥0.5：
          已授权且命中字段线索的问句跳过 Jev 的转接建议；handoff_always 命中仍优先（AC-57）
          先查预算（用完 → budget_exhausted，AC-51）；call_phase 是 live_rep/identity_check → request_handoff，
          否则 ask_for_representative；话术不含任何字段值（AC-52）
        3 menu：menu_choice ≥0.85 记 auto_high_conf，否则取最高项记 low_stakes_top（AC-49）；
          最高项 none_of_the_above → menu_safe_default；动作 press
        4 consequential：round(stakes) ≥2、命中 consequential 词表或 needs_user ≥0.5 即算；任务卡有对应拨号前决定
          → answer_precall（AC-54）；否则预算还有 → ask_user（AC-55，不看 Jev 置信度）；
          预算用完时，命中公司 skill 某题 tags 且有 default → 按默认作答（D38）；否则 budget_exhausted
          可代说字段问句只在命中 consequential 词表时进入这一步（AC-57 新裁定）
        5 standing_rules：survey/marketing → decline_offer（AC-56）
        6 preferences：prior_choices 里 source=preference、question_key 等于本轮命中的 lexicons 标签的
          低后果选择 → 照它（AC-58；PriorChoice 的 question_key 取法见 models.PriorChoice，A-03）。
          缺口 G-G-05（定稿 use_preference_pending_answer）：结果 intent="use_preference:<question_key>"
          （models.USE_PREFERENCE_PREFIX）、action=say、say_text = say_templates["pending.answer"]
          .format(option_label=<偏好的 choice，即 label 原文>, option_detail="")；paperless、language 在第 5 步已由
          常设规则回答，AC-58 用例改用常设规则之外的低后果话题（如回电时段），标签由 G 加进 lexicons
        7 confidence：按 conversation 阈值取 Jev 选择，低于 0.85 取最高项；press_field 只在数字等于档案值时放行，
          否则 ask_repeat（AC-53）；含字段线索的那一句为问句或索要祈使句时，只接受对应 press_field，
          其余选项转 open_answer（AC-57）；陈述句保留 Jev 选择；同句账户号键入请求转 press_field 并核对（AC-53）；confirm_and_end 要求转写里已有
          符合 confirmation_pattern 的号码或 call_phase=ended，否则先 ask_repeat。"""
        if request.turn is None:
            raise PolicyError("call_round requires a turn")
        config = self._points["call_round"]
        text = request.turn.text.lower()
        pick = _top(answers["menu_choice"])
        phase = _top(answers["call_phase"])
        stakes = round(answers["stakes"].score)
        needs = answers["needs_user"].noul
        consequential = config["consequential"]
        exhausted = request.context.interrupts_used >= self._global.interrupt_budget
        templates = compiled.say_templates

        def out(intent, action, reason, rule, **kwargs):
            return PolicyOutcome(intent=intent, action=action, call_phase=phase,
                                 reason_code=reason, policy_rule=f"call_round.{rule}", **kwargs)

        def budget():
            return out(config["safe_defaults"]["budget_exhausted"], "end", "budget_exhausted",
                       "budget", say_text=templates["end.goodbye"])

        def say(intent, key, rule, reason=None, **values):
            return out(intent, "say", reason or intent, rule, say_text=templates[key].format(**values))

        if self._global.never_claim_to_be_user and _hit(compiled, "identity", text):
            return say("state_ai_identity", "state_ai_identity", "identity")
        forbidden = any(_hit(compiled, key, text) for key in self._global.handoff_always)
        allowed_questions = set()
        if not forbidden:
            fields = set(compiled.allowed_fields)
            if compiled.task_card.effective_date:
                fields.add("start_date")
            allowed_questions = {key for key in fields if _hit(compiled, FIELD_CUES_PREFIX + key, text)}
        in_call_question = any(_matches(q.get("tags", []), text)
                               for q in compiled.policy.get("precall_questions", []))
        if forbidden or (not allowed_questions and not in_call_question and pick == "request_handoff"
                         and needs >= consequential["needs_user_threshold"]):
            if exhausted:
                return budget()
            if phase in {"live_rep", "identity_check"}:
                return out("request_handoff", "handoff", "forbidden_info", "forbidden_info",
                           say_text=templates["handoff.announce"])
            return say("ask_for_representative", "ask_for_representative", "forbidden_info")
        if request.turn.kind == "menu":
            if pick == NONE_OF_THE_ABOVE:
                rules = compiled.policy["ivr_rules"]
                candidates = {c.id for c in request.candidates}
                for digits in (rules["operator"], rules["repeat"]):
                    if digits in candidates:
                        return out("menu_safe_default", "press", "menu_safe_default", "menu", digits=digits)
                return say("menu_safe_default", "ask_repeat", "menu", "menu_safe_default")
            if pick not in {c.id for c in request.candidates} or not re.fullmatch(r"[0-9*#]+", pick):
                return say("ask_repeat", "ask_repeat", "menu")
            return out(pick, "press", _confidence_reason(answers["menu_choice"], config["menu"]["min_confidence"]),
                       "menu", digits=pick)
        has_consequences = (_hit(compiled, "consequential", text)
                            or (bool(allowed_questions) and _hit(compiled, "field_change", text))
                            or (not allowed_questions and (stakes >= consequential["min_stakes"]
                                or needs >= consequential["needs_user_threshold"])))
        if has_consequences:
            card = request.context.task_card
            if card:
                definitions = compiled.policy["precall_questions"]
                tags = {q["question_id"]: q["tags"] for q in definitions}
                for decision in card.precall_decisions:
                    if decision.selected is not None and _matches(tags.get(decision.question_id, []), text):
                        label = next(o.label for o in decision.options if o.id == decision.selected)
                        return say(f"answer_precall:{decision.question_id}", "answer_precall", "consequential",
                                   "answer_precall", option_label=label)
            if exhausted:
                for question in compiled.policy.get("precall_questions", []):
                    if question.get("default") is not None and _matches(question.get("tags", []), text):
                        label = next(o["label"] for o in question["options"] if o["id"] == question["default"])
                        return say(f"answer_precall:{question['question_id']}", "answer_default", "consequential",
                                   "answer_default", option_label=label)
                return budget()
            return out("ask_user", "ask_user", "consequential", "consequential", learn=self.should_learn(stakes))
        rules = {r.rule_key: r for r in request.context.standing_rules}
        if "decline_surveys" in rules and (_hit(compiled, "survey", text) or _hit(compiled, "marketing", text)):
            return say("decline_offer", "decline_offer", "standing_rules")
        for topic in ("paperless", "language"):
            if topic in rules and _hit(compiled, topic, text):
                return say(f"standing_rule:{topic}", f"standing.{topic}", "standing_rules")
        if self.should_learn(stakes):
            for prior in request.context.prior_choices:
                if (prior.source == "preference" and prior.stakes is not None and self.should_learn(prior.stakes)
                        and _hit(compiled, prior.question_key, text)):
                    return say(f"use_preference:{prior.question_key}", "pending.answer", "preferences",
                               "use_preference", option_label=prior.choice, option_detail="")
        reason = _confidence_reason(answers["menu_choice"], config["conversation"]["min_confidence"])
        sentences = re.findall(r"[^.!?]+[.!?]?", text)
        requested_fields = {key for key in allowed_questions
                            if any(_hit(compiled, FIELD_CUES_PREFIX + key, sentence)
                                   and _is_question(config, sentence) for sentence in sentences)}
        keypad_field = next((key for key in ("account_number", "phone_number")
                             if any(_hit(compiled, FIELD_CUES_PREFIX + key, sentence)
                                    and _matches(config["keypad_entry"], sentence)
                                    and _is_question(config, sentence) for sentence in sentences)), None)
        if (requested_fields and pick not in {f"press_field:{key}" for key in requested_fields}):
            pick = "open_answer"
        if keypad_field:
            # Force the requested entry, then run the same AC-53 authorization
            # checks even when the field is missing or cannot be keyed.
            pick = f"press_field:{keypad_field}"
        if pick.startswith("press_field:"):
            key = pick.split(":", 1)[1]
            value = compiled.allowed_fields.get(key)
            if (key not in allowed_questions or key not in compiled.ivr_map["keypad_fields"]
                    or _keypad_digits(key, value) is None):
                return say("ask_repeat", "ask_repeat", "confidence", "press_field_refused")
            if request.context.allowed_fields.get(key) != value:
                return say("ask_repeat", "ask_repeat", "confidence", "press_field_refused")
            digits = _keypad_digits(key, value)
            for suffix, cues in compiled.policy["ivr_rules"]["field_terminators"].items():
                if _matches(cues, text):
                    digits += suffix
                    break
            return out(pick, "press", reason, "confidence", digits=digits)
        if pick == "confirm_and_end":
            pattern = compiled.ivr_map["confirmation_pattern"]
            transcript = "\n".join(line.text for line in request.transcript)
            confirmed = re.search(r"(?<!\w)(?:" + pattern.removeprefix("^").removesuffix("$") + r")(?!\w)", transcript)
            if not confirmed and phase != "ended":
                return say("ask_repeat", "ask_repeat", "confidence", "end_without_confirmation")
            return out(pick, "end", reason, "confidence", say_text=templates["end.goodbye"])
        if pick == "ask_user":
            if exhausted:
                return budget()
            return out(pick, "ask_user", reason, "confidence", learn=self.should_learn(stakes))
        if pick == "request_handoff":
            if exhausted:
                return budget()
            if phase not in {"live_rep", "identity_check"}:
                return say("ask_for_representative", "ask_for_representative", "confidence")
            return out(pick, "handoff", reason, "confidence", say_text=templates["handoff.announce"])
        if pick.startswith("answer_precall:"):
            # No speech for a card choice without a matching company question in this turn.
            return say("ask_repeat", "ask_repeat", "confidence")
        action = config["actions"].get(pick)
        if action == "say":
            return say(pick, pick, "confidence", reason)
        if action in {"release", "wait"}:
            return out(pick, action, reason, "confidence")
        return say("ask_repeat", "ask_repeat", "confidence")

    def evaluate_point(
        self,
        point: DecisionPoint,
        request: DecisionRequest,
        answers: Mapping[str, JevAnswer],
    ) -> PolicyOutcome:
        """其余 7 个点：min_confidence 与 below_threshold（top_choice/keep_default/needs_follow_up/follow_up），
        以及 rules（如 assisted-call 的 never_auto：4 个用户动作工具在通话中 → wait_for_call，
        其余 → present_task_card，AC-23）。

        歧义表 A-02（定稿 wait_except_c2）：
        - models.WAIT_ONLY_POINTS：action 固定 wait，选择放 intent。
        - said_as_decided：said ≥ min_probability（0.7）→ intent=said_ok、action=wait；
          否则 request.attempt < retries（1）→ intent=reinject、action=say、say_text=
          request.template_vars[models.SAID_SUMMARY_VAR]；否则 intent=safe_default、reason_code=say_mismatch，
          动作取 policy.yaml 新增的 said_as_decided.safe_default（G 写，建议 end；这通结果落到 needs_follow_up）。
          DecisionResult 的校验器会拒绝别的组合。"""
        config = self._points[point]
        if point == "said_as_decided":
            [answer] = list(answers.values())
            if answer.noul >= config["min_probability"]:
                return PolicyOutcome(intent="said_ok", action="wait", reason_code="said_ok", policy_rule=point)
            if request.attempt < config["retries"]:
                return PolicyOutcome(intent="reinject", action="say", say_text=request.template_vars[SAID_SUMMARY_VAR],
                                     reason_code="reinject", policy_rule=point)
            return PolicyOutcome(intent="safe_default", action=config["safe_default"],
                                 reason_code="say_mismatch", policy_rule=point)
        [answer] = list(answers.values())
        intent = _top(answer)
        confidence = answer.confidence
        threshold = config["min_confidence"]
        reason = _confidence_reason(answer, threshold)
        policy_rule = point
        if point == "assistant_next_action":
            for rule in config.get("rules", []):
                if intent in rule.get("never_auto", []):
                    intent = (rule["instead_while_running"] if _call_is_running(request)
                              else rule["instead"])
                    reason = "user_action_required"
                    policy_rule = f"{point}.never_auto.{intent}"
                elif (intent == rule.get("poll_while_running")
                      and _running_status_without_user_action(request, intent)):
                    intent = rule["instead"]
                    policy_rule = f"{point}.poll_while_running"
        elif point in {"company_skill", "task_intent"} and intent == NONE_OF_THE_ABOVE:
            intent = "unsupported" if point == "company_skill" else "ask_clarify"
        elif point == "precall_default" and confidence < threshold:
            intent, reason = config["below_threshold"], "keep_default"
        elif point in {"call_outcome", "result_item_kind"} and confidence < threshold:
            intent, reason = config["below_threshold"], config["below_threshold"]
        return PolicyOutcome(intent=intent, action="wait", reason_code=reason, policy_rule=policy_rule)

    def should_learn(self, stakes: int) -> bool:
        """learn_from_answers.enabled 且 stakes ≤ max_stakes（1）。学到的 Preference.choice 存选项的 label 原文（G-G-05）。"""
        return (self._global.learn_from_answers["enabled"]
                and 0 <= stakes <= self._global.learn_from_answers["max_stakes"])


def _call_is_running(request: DecisionRequest) -> bool:
    """Only the last successful call tool result establishes an ongoing call (I1-4)."""
    if not request.transcript:
        return False
    try:
        result = json.loads(request.transcript[-1].text)
    except (ValueError, RecursionError):
        return False
    if not isinstance(result, dict) or result.get("is_error") is not False:
        return False
    structured = result.get("structured")
    if not isinstance(structured, dict):
        return False
    return (result.get("name") in USER_ACTION_TOOLS
            or (result.get("name") == "get_call_status" and structured.get("ended") is False))


def _running_status_without_user_action(request: DecisionRequest, tool_name: str) -> bool:
    """Inspect only A's last JSON tool result; unclear facts leave Jev's pick intact."""
    if not request.transcript:
        return False
    try:
        result = json.loads(request.transcript[-1].text)
    except (ValueError, RecursionError):
        return False
    if (not isinstance(result, dict) or result.get("name") != tool_name
            or result.get("is_error", False) is not False):
        return False
    status = result.get("structured")
    if (not isinstance(status, dict) or status.get("task_id") != request.task_id
            or status.get("ended") is not False):
        return False
    pending = status.get("pending_choice")
    if pending is not None and (not isinstance(pending, dict)
                                or pending.get("status") not in ("answered", "expired")):
        return False
    handoff = status.get("handoff")
    if handoff is not None and (not isinstance(handoff, dict)
                                or handoff.get("state") not in ("active", "ended")):
        return False
    return True


def _top(answer: JevAnswer) -> str:
    if answer.type != "choice" or not answer.probabilities:
        raise PolicyError("a choice answer with probabilities is required")
    return max(answer.probabilities, key=answer.probabilities.get)


def _confidence_reason(answer: JevAnswer, threshold: float) -> str:
    return "auto_high_conf" if answer.confidence >= threshold else "low_stakes_top"


def _hit(compiled: CompiledCall, key: str, text: str) -> bool:
    return _matches(compiled.lexicons.get(key, []), text)


def _matches(words: Sequence[str], text: str) -> bool:
    for word in words:
        if not word:
            continue
        # Word cues cannot match inside another word. Symbol cues such as '$'
        # keep matching amounts such as '$25'.
        left = r"(?<!\w)" if re.match(r"\w", word[0]) else ""
        right = r"(?!\w)" if re.match(r"\w", word[-1]) else ""
        if re.search(left + re.escape(word) + right, text, re.I):
            return True
    return False


def _is_question(config: Mapping[str, Any], text: str) -> bool:
    if "?" in text:
        return True
    text = text.lstrip()
    if any(re.match(re.escape(opener) + r"\b", text, re.I)
           for opener in config["question_openers"] if opener):
        return True
    request = re.sub(r"^(?:please|kindly)\b\s*", "", text, flags=re.I)
    return any(re.match(re.escape(opener) + r"\b", request, re.I)
               for opener in config["request_openers"] if opener)
