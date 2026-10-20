"""Privacy assertions for the five explicitly allowed gate fact types."""
from waitless.models import TRACE_ACTION_KEYS

GATE_FACTS = {'output_revoked','output_allowed','output_cleared','response_bound','response_audio'}


def assert_handoff_facts(events, *, allow_results=True):
    permitted = GATE_FACTS | ({'agent_tool_result'} if allow_results else set())
    for event in events:
        assert event.event_type in permitted
        if event.event_type in GATE_FACTS:
            assert event.transcript is None and event.speaker is None
            assert set(event.action) == set(TRACE_ACTION_KEYS[event.event_type])
            assert not ({'text','audio','keys','digits','date_of_birth'} & set(event.action))
