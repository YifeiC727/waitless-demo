"""Application events use the Sonic text channel without instructions or trace injections."""
import pytest
from waitless.call.agent_tools import event_text
from waitless.call.injector import SonicTextInjector, PipecatVersionError


def make_injector(sonic, trace, spec, clock):
    return SonicTextInjector(sonic, trace=trace, call_id=spec.call_id, task_id=spec.task_id, clock=clock)


async def test_ac71_inject_sends_prefixed_user_text_and_traces_it(make_fake_sonic, trace, spec, fake_clock):
    sonic = make_fake_sonic()
    injector = make_injector(sonic, trace, spec, fake_clock)
    data = event_text({'event': 'user_choice', 'choice_id': 'c1', 'status': 'expired'})
    await injector.send_event(data)
    assert [(i.text, i.role, i.interactive, i.status) for i in sonic.injections] == [(data, 'USER', True, 'sent')]
    assert injector.pending == 0 and not trace.of_type('injection_sent', call_id=spec.call_id)


async def test_ac71_inject_records_the_template_key(make_fake_sonic, trace, spec, fake_clock):
    sonic = make_fake_sonic()
    injector = make_injector(sonic, trace, spec, fake_clock)
    data = event_text({'event': 'handoff_ended'})
    await injector.send_event(data)
    assert sonic.sent_texts == [data]
    assert not trace.events(call_id=spec.call_id)


def test_ac71_injector_refuses_other_pipecat_versions(monkeypatch, make_fake_sonic, trace, spec, fake_clock):
    import pipecat
    make_injector(make_fake_sonic(), trace, spec, fake_clock)
    monkeypatch.setattr(pipecat, '__version__', '1.13.0')
    with pytest.raises(PipecatVersionError):
        make_injector(make_fake_sonic(), trace, spec, fake_clock)
