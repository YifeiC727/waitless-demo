import json

import pytest

from a_support import AEnv, ScriptedGate
from waitless import models as m
from waitless.assistant import _explicit_fields


PROFILE_TEXT = (
    "My name is Alex Example. My phone number is (312) 555-0147. "
    "My service address is 1200 W Example St, Apt 4B. "
    "My Lakeshore Energy account number is 100200300."
)
PROFILE_FIELDS = {
    "name": "Alex Example",
    "phone_number": "(312) 555-0147",
    "address": "1200 W Example St, Apt 4B",
    "account_number": "100200300",
}


def test_i1_profile_sentences_have_independent_values():
    assert _explicit_fields(PROFILE_TEXT) == PROFILE_FIELDS


@pytest.mark.parametrize("text, expected", [
    ("account number: 1002-0030-0.", {"account_number": "100200300"}),
    ("my account number is  1002 0030 0!", {"account_number": "100200300"}),
    ("my account number is unknown.", {}),
    ("my name is Alex Example my phone number is (312) 555-0147 "
     "my service address is 1200 W Example St, Apt 4B "
     "my Lakeshore Energy account number is 100200300", PROFILE_FIELDS),
    ("name: Alex Example; phone_number=(312) 555-0147; "
     "address: 1200 W Example St, Apt 4B; account_number=100200300", PROFILE_FIELDS),
    ("name: Alex Example\nphone_number=(312) 555-0147\n"
     "address: 1200 W Example St, Apt 4B\naccount_number=100200300", PROFILE_FIELDS),
    ("my service address is 1200 W. Example St., Apt 4B.",
     {"address": "1200 W. Example St., Apt 4B"}),
    ("address: 1200 Example St. 4B.", {"address": "1200 Example St. 4B"}),
    ("address: 1200 Example St. apt 4B.", {"address": "1200 Example St. apt 4B"}),
    ("name: Alex Example. Please continue.", {"name": "Alex Example"}),
    ("phone: (312) 555-0147? Please continue.", {"phone_number": "(312) 555-0147"}),
    ("name: Alex Example! Please continue.", {"name": "Alex Example"}),
    ("name: Alex Example. please continue.", {"name": "Alex Example. please continue"}),
    ("name: Alex Example phone number: (312) 555-0147 account number: 100200300.",
     {"name": "Alex Example", "phone_number": "(312) 555-0147", "account_number": "100200300"}),
])
def test_explicit_field_boundaries_and_account_digits(text, expected):
    assert _explicit_fields(text) == expected


def test_json_profile_values_are_unchanged():
    fields = {"name": "Alex Example.", "phone_number": "(312) 555-0147.",
              "address": "1200 W. Example St., Apt 4B.", "account_number": "1002-0030-0."}
    assert _explicit_fields(json.dumps({**fields, "unlisted": "ignore", "ssn": "ignore"})) == fields


async def test_i1_followup_builds_update_task_with_clean_profile(tmp_path):
    env = AEnv(tmp_path)
    service = env.assistant(gate=ScriptedGate({"assistant_next_action": "update_task"}))
    try:
        out = await service.turn(m.AssistantTurnIn(
            conversation_id="profile-followup", task_id="task-i1", user_text=PROFILE_TEXT,
            tools=[m.ToolInfo(name="update_task", description="Update explicit profile fields")]))
        assert out.next.kind == "tool_call"
        assert out.next.name == "update_task"
        assert out.next.arguments == {"task_id": "task-i1", "fields": PROFILE_FIELDS}
        assert service.state("profile-followup").current_fields == PROFILE_FIELDS
    finally:
        env.close()
