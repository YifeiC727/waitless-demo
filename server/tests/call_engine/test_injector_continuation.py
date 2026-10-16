"""Data events survive continuation: FIFO delivery exactly once, including abandonment."""
from waitless.call.agent_tools import event_text
from call_engine.test_injector import make_injector


async def test_ac73_injection_during_stream_swap_waits_for_the_new_stream(make_fake_sonic, trace, spec, fake_clock):
    sonic = make_fake_sonic()
    injector = make_injector(sonic, trace, spec, fake_clock)
    data = event_text({'event': 'handoff_ended'})
    sonic.begin_continuation()
    await injector.send_event(data)
    assert injector.pending == 1 and not sonic.injections
    assert not injector.stream_ready()
    assert await injector.flush() == 0
    sonic.complete_continuation()
    assert injector.stream_ready()
    assert await injector.flush() == 1
    assert [(i.text, i.status, i.generation) for i in sonic.injections] == [(data, 'sent', 2)]
    assert await injector.flush() == 0 and len(sonic.injections) == 1
    assert not trace.events(call_id=spec.call_id)


async def test_ac73_abandoned_continuation_sends_queued_text_on_the_old_stream_once(make_fake_sonic, trace, spec, fake_clock):
    sonic = make_fake_sonic()
    injector = make_injector(sonic, trace, spec, fake_clock)
    data = event_text({'event': 'handoff_ended'})
    sonic.begin_continuation()
    await injector.send_event(data)
    assert await injector.flush() == 0
    sonic.clear_handoff_flag()
    assert await injector.flush() == 1
    assert [(i.text, i.status, i.generation) for i in sonic.injections] == [(data, 'sent', 1)]
    for _ in range(3):
        assert await injector.flush() == 0
    assert not trace.events(call_id=spec.call_id)


async def test_ac73_queued_injections_go_out_once_each_in_order(make_fake_sonic, trace, spec, fake_clock):
    sonic = make_fake_sonic()
    injector = make_injector(sonic, trace, spec, fake_clock)
    data = [event_text({'event': 'user_choice', 'choice_id': str(i), 'status': 'expired'}) for i in range(4)]
    await injector.send_event(data[0])
    sonic.begin_continuation()
    for item in data[1:]:
        await injector.send_event(item)
    assert injector.pending == 3
    sonic.complete_continuation()
    assert await injector.flush() == 3
    assert await injector.flush() == 0
    assert sonic.sent_texts == data
    assert [i.generation for i in sonic.injections] == [1, 2, 2, 2]
    assert not trace.events(call_id=spec.call_id)
