from e_support import build_settings, make_rig, run_until, spec_end, spec_handoff
from tests.lane_B.test_u112_deposit_window import DOB, OK, DEPOSIT, AGREE, result
from waitless.company.text import TextCompany


def company_at_handoff(root, config):
    from waitless.company.config import load_company, load_scenario
    scenario = load_scenario(root, 'lakeshore-energy', 'baseline')
    company = TextCompany(scenario, load_company(root, 'lakeshore-energy'))
    lines = company.start()
    for key in scenario.expected.menu_digits:
        lines = company.on_digits(key)
    for _ in range(20):
        line = lines[-1]
        if line.expect.intent == 'request_handoff':
            return company, line
        if line.expect.intent.startswith('press_field:'):
            lines = company.on_digits('3125550147#')
        else:
            lines = company.on_speech('My home. No, continue by phone.')
    raise AssertionError('No handoff request')


def test_u112_text_1_join_leave_smoke(company_dir, engine_config):
    company, _ = company_at_handoff(company_dir, engine_config)
    assert [line.text for line in company.on_handoff(True)] == [DOB]
    result(company._engine, company.on_handoff(False), 'confirmed')


def test_u112_text_2_holder_input_order(company_dir, engine_config):
    company, _ = company_at_handoff(company_dir, engine_config)
    assert [line.text for line in company.on_handoff(True)] == [DOB]
    assert [line.text for line in company._engine.holder_input(speech='My date of birth on file.')] == [OK, DEPOSIT]
    assert [line.text for line in company._engine.holder_input(keys='1')] == [AGREE]
    result(company._engine, company.on_handoff(False), 'confirmed')


def test_u112_text_3_declined_never_overridden(company_dir, engine_config):
    company, _ = company_at_handoff(company_dir, engine_config)
    company.on_handoff(True)
    company._engine.holder_input(speech='My date of birth on file.')
    company._engine.holder_input(keys='2')
    result(company._engine, company.on_handoff(False), 'needs_follow_up')
    assert company.on_handoff(False) == []
    assert company._engine.confirmation_number() is None


def test_u112_text_4_dob_then_leave(company_dir, engine_config):
    company, _ = company_at_handoff(company_dir, engine_config)
    company.on_handoff(True)
    company._engine.holder_input(speech='My date of birth on file.')
    # New text-only smoke shortcut, not evidence of consent. The same scripted
    # case is needs_follow_up in test_u112_win_4_unanswered_or_early_return.
    result(company._engine, company.on_handoff(False), 'confirmed')


async def test_u112_text_5_input_outside_window_ignored(company_dir, engine_config, mode_env, fake_clock, test_profile):
    company, req = company_at_handoff(company_dir, engine_config)
    engine = company._engine
    before = (engine.step_log(), engine.confirmation_number(), engine._index)
    assert engine.holder_input(speech='Private reply', keys='1') == []
    assert before == (engine.step_log(), engine.confirmation_number(), engine._index)
    company.start = lambda: [req]
    rig = make_rig(settings=build_settings(mode_env('text-sim'), engine_config), clock=fake_clock,
                   company=company, allowed_fields=test_profile,
                   script=[('bring them onto the line', spec_handoff()), ('confirmation number', spec_end())])
    session = await rig.start()
    await run_until(fake_clock, lambda: session.status().phase == 'handoff_ready', what='handoff')
    joined = await session.join()
    private = engine.holder_input(speech='Private reply') + engine.holder_input(keys='1')
    assert [line.text for line in private] == [OK, DEPOSIT, AGREE]
    await fake_clock.advance(100)
    private_texts = {DOB, *(line.text for line in private)}
    assert not private_texts.intersection(turn.text for turn in rig.gate.turns())
    assert not private_texts.intersection(event.transcript for event in rig.events())
    assert session.status().transcript_tail == []
    started = rig.events('handoff_started')[0].seq
    assert [event for event in rig.events() if event.seq > started] == []
    await session.return_to_agent(joined.handoff_id)
    await run_until(fake_clock, lambda: session.ended, what='call end')
    before = (engine.step_log(), engine.confirmation_number(), engine.ended)
    assert engine.holder_input(speech='Private reply', keys='2') == []
    assert before == (engine.step_log(), engine.confirmation_number(), engine.ended)
