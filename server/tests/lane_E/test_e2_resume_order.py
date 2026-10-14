"""E-F5：接回时先对坐席说 handoff.resume，再让文本公司接着说（坐席的下一句不会被接回话术当成答复）。"""

from e_support import (
    SAY_TEMPLATES, ScriptedCompany, When, build_settings, line, make_rig, queue_reactions,
    run_until, spec_end, spec_handoff, spec_say,
)

DEMAND = "Before we go any further, I need the account holder on this call to verify their date of birth."
QUESTION = "Thank you, the account holder is verified. Would they prefer a refundable deposit or autopay?"
ANSWER = "The account holder chose: Pay a security deposit."
CONFIRM = "Your service request is confirmed. Your confirmation number is LE-12345."


async def test_resume_is_said_before_the_representative_continues(mode_env, engine_config, fake_clock):
    company = ScriptedCompany(
        [line(DEMAND, role="rep")],
        queue_reactions(
            When("handoff", True, [line("Please state your date of birth.", role="rep")]),
            When("handoff", False, [line(QUESTION, role="rep")]),
            When("speech", ANSWER, [line(CONFIRM, role="rep", confirmation_number="LE-12345")]),
        ),
    )
    rig = make_rig(
        settings=build_settings(mode_env("text-sim"), engine_config), clock=fake_clock, company=company,
        allowed_fields={},
        script=[("date of birth", spec_handoff()),
                ("deposit or autopay", spec_say(ANSWER, intent="answer_precall:deposit_or_autopay")),
                ("confirmation number", spec_end())],
    )
    session = await rig.start()
    await run_until(fake_clock, lambda: session.status().phase == "handoff_ready", what="handoff_ready")
    joined = await session.join()
    await session.return_to_agent(joined.handoff_id)
    await run_until(fake_clock, lambda: session.ended, what="call end")

    said_or_handoff = [(k, v) for k, v in company.inputs if k in ("speech", "handoff")]
    assert said_or_handoff.index(("speech", SAY_TEMPLATES["handoff.resume"])) < said_or_handoff.index(("handoff", False))
    turns = [t.text for t in rig.gate.turns()]
    assert turns.index(QUESTION) < turns.index(CONFIRM)
    assert company.heard().index(ANSWER) > company.heard().index(SAY_TEMPLATES["handoff.resume"])
