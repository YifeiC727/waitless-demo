"""I2E-3: spoken L4 candidates require matching company-turn evidence."""
import json

import pytest

from a_support import AEnv, Pick, RecordingLlm, ScriptedGate
from waitless import models as m

LINES = [('company',
  'welcome to lakeshore energy for service press one for billing press two for other inquiries '
  'press three to repeat this menu press nine'),
 ('company',
  'to start service press one to stop service press two to move existing service press three'),
 ('company', 'what is the service address?'),
 ('waitless', 'The service address is 1200 W Example St, Apt 4B.'),
 ('company', 'on what date should service start?'),
 ('waitless', 'The requested start date is November 1.'),
 ('company', 'please enter your account number, followed by the pound key.'),
 ('company',
  'your new service needs a special approval. let me transfer you to a representative. am i '
  'speaking with the account holder?'),
 ('waitless', 'One moment, please. Let me check that with the account holder.'),
 ('waitless', ' \n\nI am an AI assistant calling on behalf of Alex Example.'),
 ('waitless',
  'One moment, please. I am an AI assistant calling on behalf of the account holder, not the '
  'account holder.'),
 ('company',
  'i need the account holder on this same call to verify their identity. please bring them onto '
  'the line.'),
 ('waitless', 'One moment, please. Let me check that with the account holder.'),
 ('waitless',
  'One moment, please. Thank you for verifying. I’ll now proceed with starting electric service at '
  '1200 W Example St, Apt 4B for an effective date of November 1'),
 ('waitless', ' with a security deposit.'),
 ('company',
  'verification is complete. would the account holder prefer a refundable deposit or auto pay?'),
 ('waitless', 'One moment, please. The account holder has chosen to pay a security deposit.'),
 ('company',
  'your service request is confirmed. your confirmation number is l e sixty five thousand eight '
  'hundred thirty three. thank you.'),
 ('waitless',
  'One moment, please. Thank you. The service request is confirmed with confirmation number LE '
  '65833.')]

SUMMARY = ('Lakeshore Energy confirmed the service request with confirmation number LE 65833. Identity '
 'verification was completed, and the account holder selected a security deposit for the requested '
 'November 1 service start at 1200 W Example St, Apt 4B. No unresolved follow-up is evident in the '
 'transcript.')


@pytest.fixture
def env(tmp_path):
    instance = AEnv(tmp_path)
    yield instance
    instance.close()


async def finalize(env, numbers, *, lines=LINES, summary="The call finished.", pick=None, statements=()):
    task_id = env.add_task("task-r12", status="in_call")
    env.add_call(task_id, "call-r12", lines)
    llm = RecordingLlm(result_items=m.ResultItems(confirmation_numbers=numbers, statements=list(statements)),
                       summary=summary)
    gate = ScriptedGate({"call_outcome": pick or Pick("confirmed", confidence=0.97),
                         "result_item_kind": "follow_up"})
    out = await env.result_service(gate=gate, llm=llm).finalize("call-r12")
    [row] = env.store.list_calls(task_id)
    stored = json.loads(row["result_json"])
    assert stored == out.model_dump(mode="json")
    assert row["outcome"] == out.outcome
    assert row["confirmation_number"] == out.confirmation_number
    return out, row, llm


@pytest.mark.parametrize("candidate", [
    "l e sixty five thousand eight hundred thirty three", "LE 65833",
    "l e six five eight three three", "l e sixty five eight thirty three",
])
async def test_r12_spoken_candidate_matches_company_evidence(env, candidate):
    out, row, _ = await finalize(env, [candidate])
    assert out.outcome == row["outcome"] == "confirmed"
    assert out.confirmation_number == row["confirmation_number"] == "LE-65833"


@pytest.mark.parametrize("lines", [
    [("company", "We still need to check the request."), LINES[-1]],
    [("company", "Your number is LE-12345."), LINES[-1]],
    [("waitless", LINES[-2][1]), LINES[-1]],
])
async def test_r12_guard_requires_same_number_in_company_turn(env, lines):
    out, _, _ = await finalize(env, ["LE 65833"], lines=lines)
    assert (out.outcome, out.confirmation_number) == ("needs_follow_up", None)


@pytest.mark.parametrize("numbers", [["48291"], ["l e one two three"], []])
async def test_r12_guard_does_not_invent_missing_letters_digits_or_l4_candidate(env, numbers):
    out, _, _ = await finalize(env, numbers)
    assert (out.outcome, out.confirmation_number) == ("needs_follow_up", None)


async def test_r12_guard_canonical_l4_path_stays_unchanged(env):
    out, _, _ = await finalize(env, ["LE 65833", "LE-48291"],
                              lines=[("company", "Your confirmation number is LE-48291.")])
    assert (out.outcome, out.confirmation_number) == ("confirmed", "LE-48291")


@pytest.mark.parametrize("pick", [Pick("confirmed", confidence=0.84),
                                   Pick("needs_follow_up", confidence=0.97)])
async def test_r12_guard_number_does_not_override_outcome_decision(env, pick):
    out, _, _ = await finalize(env, ["LE 65833"], pick=pick)
    assert (out.outcome, out.confirmation_number) == ("needs_follow_up", None)


@pytest.mark.parametrize(("text", "expected"), [
    (LINES[-2][1], ["LE-65833"]),
    ("LE 65833", ["LE-65833"]), ("LE-65833", ["LE-65833"]),
    ("l e six five eight three three", ["LE-65833"]),
    ("l e sixty five eight thirty three", ["LE-65833"]),
    ("le sixty-five thousand eight hundred and thirty-three", ["LE-65833"]),
    ("LE 65 eight thirty three", ["LE-65833"]),
    ("LE oh zero one two three", ["LE-00123"]),
    ("LE 00123", ["LE-00123"]),
    ("LE nineteen thousand nine hundred ninety nine", ["LE-19999"]),
    ("LE ninety nine thousand nine hundred ninety nine", ["LE-99999"]),
    ("48291", []), ("e l 12345", []), ("l e one two three", []),
    ("LE 658330", []), ("LE six five eight three three nine", []),
])
def test_r12_normalize_confirmation_numbers(text, expected):
    from waitless.results import _confirmation_candidates
    assert _confirmation_candidates(text, r"^LE-\d{5}$") == expected


@pytest.mark.parametrize(("pattern", "expected"), [
    (r"^AB\d{5}$", ["AB12345"]),
    (r"^AB/\d{5}$", ["AB/12345"]),
    (r"^AB_\d{5}$", ["AB_12345"]),
    (r"^AB\.\d{5}$", ["AB.12345"]),
    (r"^AB:\d{5}$", ["AB:12345"]),
    (r"^AB \d{5}$", ["AB 12345"]),
    (r"^CD-\d{5}$", []),
])
def test_r12_separators_and_prefix_come_from_company_pattern(pattern, expected):
    from waitless.results import _confirmation_candidates
    assert _confirmation_candidates("a b one two three four five", pattern) == expected


@pytest.mark.parametrize("pattern", [r"^(?:AB.)\d{5}$", r"^(?i:AB.)\d{5}$",
                                     r"^AB(?P<separator>.)\d{5}$"])
def test_r12_regex_group_syntax_is_not_a_literal_separator(pattern):
    from waitless.results import _confirmation_candidates
    assert _confirmation_candidates("AB 12345", pattern) == []


@pytest.mark.parametrize("text", ["AB hundred", "AB thousand"])
def test_r12_guard_scale_does_not_supply_unspoken_one(text):
    from waitless.results import _confirmation_candidates
    assert _confirmation_candidates(text, r"^AB-\d{3,4}$") == []
