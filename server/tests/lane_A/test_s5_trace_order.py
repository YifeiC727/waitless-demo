from a_support import AEnv, consequential, decision_event


def test_trace_check_requires_decision_to_precede_action(tmp_path):
    env = AEnv(tmp_path)
    try:
        env.store.insert_turn(consequential("dtmf_sent", "task", "call", "late"))
        env.trace.event(decision_event("task", "call", "late", turn_seq=1))
        env.trace.event(consequential("dtmf_sent", "task", "call", "late"))
        violations = env.trace.check_call("call")
        assert len(violations) == 1
        assert violations[0].seq == 1
        assert violations[0].event_type == "dtmf_sent"
    finally:
        env.close()
