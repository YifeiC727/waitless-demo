from lane_D.agent_support import *


async def test_ac64_registers_the_six_skill_tools(agent_call):
    h = agent_call
    await h.boot()
    mod = agent_module()
    names = ('press_keys', 'update_status', 'ask_user', 'ask_jev', 'request_handoff', 'end_call')
    assert tuple(mod.TOOL_NAMES) == names
    assert set(h.sonic.registered_functions) == set(names)
    schemas = {s.name: s.to_default_dict() for s in h.sonic.tools.standard_tools}
    assert set(schemas) == set(names)
    for name in names:
        assert schemas[name]['description'] == h.spec.compiled.agent_tool_descriptions[name]
        assert schemas[name]['parameters'] == mod.TOOL_PARAMETERS[name]
        assert h.sonic.registrations[name].get('cancel_on_interruption') is not False
    assert schemas['press_keys']['parameters']['properties']['digits']['pattern'] == '^[0-9*#]{1,32}$'
    # Exercise the real constructor too: continuation must survive adding tools.
    from waitless.call.pipeline import TappedNovaSonicService
    from pipecat.services.aws.nova_sonic.session_continuation import SessionContinuationParams
    svc = TappedNovaSonicService(model=h.settings.sonic_model_id, voice_id=h.settings.sonic_voice,
        region=h.settings.aws_region, access_key_id='test-not-a-key', secret_access_key='test-not-a-secret',
        system_instruction=h.spec.compiled.sonic_prompt, tools=h.sonic.tools,
        session_continuation=SessionContinuationParams(enabled=True))
    h.call._toolbox.register(svc)
    assert not svc._has_async_tools()
    assert svc._settings.system_instruction == h.spec.compiled.sonic_prompt
