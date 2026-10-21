"""AC-47：每轮一次 Jev 请求问 4 题；候选与 state 的组成（G 线；tech §6.5、§6.6、§7.1；第 3 步可见测试）。

经 DecisionGate.decide_round(turn, ctx) 驱动（ctx 是 models.RoundContext，歧义定稿 A-01），
用 FakeJev 截获请求；另测 round_candidates()、parse_menu()、truncate_label()。
"""

import json
from pathlib import Path

import pytest

from fakes import round_answers
from g_support import (
    ADDRESS_TEXT,
    CALL_ID,
    JEV_MODEL,
    MENU_OPTIONS,
    MENU_TEXT,
    NEUTRAL_TEXT,
    TASK_ID,
    PointChooser,
    build_kit,
    line,
    menu_turn,
    point_questions,
    prefer,
    press_patterns,
    profile,
    read_yaml,
    require_skills_dir,
    speech_turn,
    standing_rules,
    strip_none,
    task_card,
    tool_candidates,
)
from waitless.decision.gate import round_candidates
from waitless.decision.jev import JevError
from waitless.decision.menu_parse import MENU_LABEL_MAX, parse_menu, truncate_label
from waitless.models import (
    ANSWER_PRECALL_PREFIX,
    CALL_ROUND_QUESTIONS,
    JEV_TRANSCRIPT_NOTE,
    NONE_OF_THE_ABOVE,
    PRESS_FIELD_PREFIX,
    ROUND_INTENTS,
    USER_ACTION_TOOLS,
    PriorChoice,
)
from waitless.skills.compiler import compile_call
from waitless.skills.loader import load_skills

LONG_LABEL = "For questions about " + "an unusually long description of this option " * 4


def _kit(skills_dir, test_profile, fake_jev, fake_clock, **kw):
    return build_kit(skills_dir, jev=fake_jev, clock=fake_clock, prof=profile(test_profile), **kw)


async def test_ac47_menu_round_asks_four_questions_with_parsed_options(
    skills_dir: Path, test_profile: dict, fake_jev, fake_clock
) -> None:
    kit = _kit(skills_dir, test_profile, fake_jev, fake_clock)
    options = [*MENU_OPTIONS, ("3", LONG_LABEL)]
    fake_jev.push(round_answers("1", call_phase="ivr_menu"))
    result = await kit.round(menu_turn(MENU_TEXT, options), phase="ivr")

    assert len(fake_jev.calls) == 1
    call = fake_jev.calls[0]
    assert {qid: q.type for qid, q in call.questions.items()} == CALL_ROUND_QUESTIONS
    menu_q = call.questions["menu_choice"]
    assert set(menu_q.criteria) == {"1", "2", "9", "3", NONE_OF_THE_ABOVE}
    for digits, _label in options:
        assert len(menu_q.criteria[digits]) <= MENU_LABEL_MAX
    assert menu_q.criteria["3"][:30] == LONG_LABEL[:30]

    spec = point_questions(skills_dir, "call_round")
    assert menu_q.instructions == spec["menu_choice"]["instructions_menu"]
    for qid in ("call_phase", "needs_user", "stakes"):
        assert call.questions[qid].instructions == spec[qid]["instructions"]
    assert call.questions["call_phase"].criteria == spec["call_phase"]["criteria"]
    assert call.questions["stakes"].criteria == spec["stakes"]["criteria"]

    assert result.action == "press" and result.digits == "1"
    assert result.call_phase == "ivr_menu"


async def test_ac47_conversation_round_offers_the_intents(
    skills_dir: Path, test_profile: dict, fake_jev, fake_clock
) -> None:
    kit = _kit(skills_dir, test_profile, fake_jev, fake_clock)
    fake_jev.push(round_answers("open_answer", call_phase="ai_agent"))
    await kit.round(speech_turn(ADDRESS_TEXT), phase="ai_agent")

    menu_q = fake_jev.calls[0].questions["menu_choice"]
    keys = set(menu_q.criteria)
    assert set(ROUND_INTENTS) <= keys
    assert "press_field:account_number" in keys, "profile has an account number and the company skill keys it"
    assert "answer_precall:deposit_or_autopay" in keys, "the task card carries this pre-call decision"
    assert NONE_OF_THE_ABOVE not in keys
    for extra in keys - set(ROUND_INTENTS):
        if extra.startswith(PRESS_FIELD_PREFIX):
            assert extra[len(PRESS_FIELD_PREFIX):] in kit.compiled.allowed_fields
        else:
            assert extra == ANSWER_PRECALL_PREFIX + "deposit_or_autopay", f"unexpected candidate {extra}"
    spec = point_questions(skills_dir, "call_round")
    assert menu_q.instructions == spec["menu_choice"]["instructions_conversation"]


async def test_ac47_state_has_the_five_parts(skills_dir: Path, test_profile: dict, fake_jev, fake_clock) -> None:
    kit = _kit(skills_dir, test_profile, fake_jev, fake_clock)
    turn = speech_turn(NEUTRAL_TEXT, seq=12)
    transcript = [line("company" if i % 2 else "waitless", f"earlier line {i}") for i in range(11)]
    transcript.append(line("company", turn.text))
    prior = [PriorChoice(question_key="deposit_or_autopay", choice="deposit", stakes=3, source="task_card")]
    fake_jev.push(round_answers("open_answer", call_phase="live_rep"))
    await kit.round(turn, transcript=transcript, prior_choices=prior)

    state = fake_jev.calls[0].state
    assert state.task_goal == kit.compiled.task_card.goal
    assert state.user_profile, "user_profile carries the speakable fields"
    for key, value in state.user_profile.items():
        assert kit.compiled.allowed_fields.get(key) == value
    assert state.standing_rules == [r.rule_text for r in kit.rules]  # 歧义定稿 A-26
    assert state.prior_choices == prior
    assert state.transcript.note == JEV_TRANSCRIPT_NOTE
    assert state.transcript.lines == transcript[-8:]
    assert state.transcript.lines[-1].text == turn.text


async def test_ac47_state_for_a_point_before_the_task_card(
    skills_dir: Path, test_profile: dict, fake_jev, fake_clock
) -> None:
    """非通话决策点也用同一份 state（A-01、A-26）：没有任务卡时 task_goal 取 registry.default_task_goal，
    它写在 skills/assisted-call/jev/questions.yaml 顶层；用户原话以 speaker=user 进 transcript。"""
    kit = _kit(skills_dir, test_profile, fake_jev, fake_clock)
    fake_jev.push(PointChooser(prefer("prepare_task", exclude=(NONE_OF_THE_ABOVE, *USER_ACTION_TOOLS))))
    said = "Please start electricity at my new apartment on November 1."
    await kit.decide("assistant_next_action", candidates=tool_candidates(), transcript=[line("user", said)])
    state = fake_jev.calls[0].state
    goal = read_yaml(skills_dir / "assisted-call" / "jev" / "questions.yaml").get("default_task_goal")
    assert isinstance(goal, str) and goal.strip()
    assert kit.registry.default_task_goal == goal
    assert state.task_goal == goal
    assert state.standing_rules == [r.rule_text for r in kit.rules]
    assert state.transcript.lines == [line("user", said)]
    assert said not in json.dumps({q: v.instructions for q, v in fake_jev.calls[0].questions.items()})


async def test_ac47_decision_is_recorded_before_returning(
    skills_dir: Path, test_profile: dict, fake_jev, fake_clock
) -> None:
    kit = _kit(skills_dir, test_profile, fake_jev, fake_clock)
    turn = menu_turn(seq=3)
    fake_jev.push(round_answers("2", call_phase="ivr_menu"))
    result = await kit.round(turn, phase="ivr")

    assert result.decision_id == "dec-1", "decision ids come from the injected id source"
    assert kit.trace.has_decision(result.decision_id)
    [event] = kit.trace.decisions()
    assert event.decision_id == result.decision_id
    assert event.decision_point == "call_round"
    assert event.call_id == CALL_ID and event.task_id == TASK_ID
    assert event.jev_request["model"] == JEV_MODEL
    assert set(event.jev_request["questions"]) == set(CALL_ROUND_QUESTIONS)
    assert strip_none(event.jev_request["state"]) == strip_none(fake_jev.calls[0].state.model_dump(mode="json"))
    assert event.jev_response["answers"]["menu_choice"]["choice"] == "2"
    assert event.policy_decision and result.reason_code in json.dumps(event.policy_decision)
    assert event.latency_ms is not None and event.latency_ms >= 0
    assert (event.action or {}).get("turn_seq") == turn.seq  # 歧义定稿 A-08
    assert result.turn_seq == turn.seq  # 缺口 D-G-2：执行器的 release 按它找那一轮
    assert result.jev.request.model == JEV_MODEL
    assert set(result.jev.request.questions) == set(CALL_ROUND_QUESTIONS)
    assert result.jev.response == fake_jev.calls[0].response


async def test_ac47_jev_failure_makes_no_decision(skills_dir: Path, test_profile: dict, fake_jev, fake_clock) -> None:
    kit = _kit(skills_dir, test_profile, fake_jev, fake_clock)
    fake_jev.push(JevError("Jev unavailable"))
    with pytest.raises(JevError):
        await kit.round(speech_turn(NEUTRAL_TEXT))
    assert kit.trace.decisions() == [], "no decision may be recorded or returned without a Jev answer"


def test_ac47_round_candidates(skills_dir: Path, test_profile: dict) -> None:
    skills = load_skills(require_skills_dir(skills_dir))
    compiled = compile_call(task_card(), skills, profile(test_profile), standing_rules(), [])
    menu = round_candidates(menu_turn(), compiled)
    assert [c.id for c in menu] == ["1", "2", "9", NONE_OF_THE_ABOVE]
    speech = {c.id for c in round_candidates(speech_turn(ADDRESS_TEXT), compiled)}
    assert set(ROUND_INTENTS) <= speech
    assert NONE_OF_THE_ABOVE not in speech


def test_ac47_parse_menu_reads_both_phrasings(skills_dir: Path) -> None:
    patterns = press_patterns(require_skills_dir(skills_dir))
    opts = parse_menu(
        "To start, stop, or move service, press 1. For billing questions, press 2. For power outages, press 3.",
        patterns,
    )
    assert [o.digits for o in opts] == ["1", "2", "3"]
    assert "start" in opts[0].label.lower()
    assert "billing" in opts[1].label.lower()
    assert "outage" in opts[2].label.lower()
    opts = parse_menu("Press 1 for new service. Press 2 for billing.", patterns)
    assert [o.digits for o in opts] == ["1", "2"]
    assert "new service" in opts[0].label.lower()


def test_ac47_parse_menu_number_words_and_duplicates(skills_dir: Path) -> None:
    patterns = press_patterns(require_skills_dir(skills_dir))
    opts = parse_menu(
        "For new service, press one. For billing, press two. To repeat this menu, press nine. "
        "For new service, press one.",
        patterns,
    )
    assert [o.digits for o in opts] == ["1", "2", "9"]
    assert "new service" in opts[0].label.lower()


def test_ac47_parse_menu_without_options_is_empty(skills_dir: Path) -> None:
    patterns = press_patterns(require_skills_dir(skills_dir))
    assert parse_menu("Please hold while we connect you to the next available agent.", patterns) == []


def test_ac47_truncate_label() -> None:
    assert MENU_LABEL_MAX == 80
    assert truncate_label("For billing questions") == "For billing questions"
    cut = truncate_label(LONG_LABEL)
    assert len(cut) <= 80
    assert cut[:40] == LONG_LABEL[:40]


def test_ac47_parse_menu_uses_the_compiled_digit_words(skills_dir: Path, test_profile: dict) -> None:
    """缺口 G-G-03：调用方按 parse_menu(text, compiled.press_patterns, digit_words=compiled.digit_words) 调用。"""
    skills = load_skills(require_skills_dir(skills_dir))
    compiled = compile_call(task_card(), skills, profile(test_profile), standing_rules(), [])
    opts = parse_menu(
        "For billing, press two. To hear these options again, press nine.",
        compiled.press_patterns,
        digit_words=compiled.digit_words,
    )
    assert [o.digits for o in opts] == ["2", "9"]
