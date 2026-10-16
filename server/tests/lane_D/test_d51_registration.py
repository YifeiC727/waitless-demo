from lane_D.agent_support import *


async def test_ac64_registers_the_five_skill_tools(agent_call):
    h = agent_call
    await h.boot()
    mod = agent_module()
    names = ('update_status', 'ask_user', 'ask_jev', 'request_handoff', 'end_call')
    assert tuple(mod.TOOL_NAMES) == names
    assert set(h.sonic.registered_functions) == set(names)
    schemas = {s.name: s.to_default_dict() for s in h.sonic.tools.standard_tools}
    assert set(schemas) == set(names)
    for name in names:
        assert schemas[name]['description'] == h.spec.compiled.agent_tool_descriptions[name]
        assert schemas[name]['parameters'] == mod.TOOL_PARAMETERS[name]
        assert h.sonic.registrations[name].get('cancel_on_interruption') is not False
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


@pytest.mark.parametrize('bot_speaking,tool_name,args,want', [
    (False, 'update_status', {'phase': 'hold', 'status_line': 'Waiting'}, {'ok': True}),
    (True, 'update_status', {'phase': 'hold', 'status_line': 'Waiting'}, {'ok': True}),
    (False, 'request_handoff', {'reason': 'Verification'}, {'status': 'handoff_ready'}),
])
async def test_tool_result_returns_through_real_aggregator_to_sonic(agent_call, monkeypatch, bot_speaking, tool_name, args, want):
    """Catch removal of the assistant aggregator or context feedback, offline."""
    import json
    from waitless.call import pipeline
    from pipecat.frames.frames import BotStartedSpeakingFrame, BotStoppedSpeakingFrame
    from pipecat.services.llm_service import FunctionCallFromLLM
    from pipecat.services.aws.nova_sonic.session_continuation import SessionContinuationParams
    sent = []
    async def offline_connect(self):
        pass
    async def send_result(tool_id, result):
        sent.append((tool_id, json.loads(result)))
    monkeypatch.setattr(pipeline.TappedNovaSonicService, '_start_connecting', offline_connect)
    svc = pipeline.TappedNovaSonicService(model=agent_call.settings.sonic_model_id,
        voice_id=agent_call.settings.sonic_voice, region=agent_call.settings.aws_region,
        access_key_id='test-not-a-key', secret_access_key='test-not-a-secret',
        system_instruction=agent_call.spec.compiled.sonic_prompt,
        session_continuation=SessionContinuationParams(enabled=True))
    monkeypatch.setattr(svc, '_send_tool_result', send_result)
    monkeypatch.setattr(pipeline, 'build_sonic_service', lambda compiled, settings, **kwargs: svc)
    h = agent_call
    await h.boot()
    await wait_until(lambda: svc._context is not None)
    def walk(processor):
        yield processor
        for child in getattr(processor, 'processors', []):
            yield from walk(child)
    aggregators = [p for p in walk(h.call._task._pipeline)
                   if type(p).__name__ in ('LLMUserAggregator', 'LLMAssistantAggregator')]
    assert len(aggregators) == 2 and all(p._realtime_service_mode is True for p in aggregators)
    if bot_speaking:
        await svc.push_frame(BotStartedSpeakingFrame())
        await settle(200)
    await svc.run_function_calls([FunctionCallFromLLM(context=svc._context,
        tool_call_id='tool-result-check', function_name=tool_name,
        arguments=args)])
    await settle(300)
    if tool_name == 'request_handoff':
        await h.clock.advance(h.settings.engine.handoff.announce_idle_ms + 1)
        await settle(300)
    if bot_speaking:
        assert sent == []
        await svc.push_frame(BotStoppedSpeakingFrame())
    await wait_until(lambda: sent, what='real aggregator feedback to Sonic')
    assert sent == [('tool-result-check', want)]
    assert svc._settings.system_instruction == h.spec.compiled.sonic_prompt
