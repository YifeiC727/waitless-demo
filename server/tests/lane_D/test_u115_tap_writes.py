"""E174 keeps f0aca90 transcript writes and releases only on real speech."""
import asyncio

import pytest

from call_engine._support import settle
from lane_D.agent_support import compiled, engine, line_number, settings, spec, store
from lane_D.test_u115_repeat_hold import repeat_call


async def held_repeat(h):
    assert await h.invoke() == {'ok': True}
    task, received = await h.start()
    assert len(h.events('agent_tool_held')) == 1
    assert not task.done() and received == []
    return task, received


@pytest.mark.parametrize('response', [None, 1])
async def test_assistant_event_final_is_written_without_releasing(repeat_call, response):
    h = repeat_call
    task, received = await held_repeat(h)
    text = '[Waitless event] {"event":"keys_pressed"}'

    await h.speech(text, role='ASSISTANT', response=response)
    if response is not None:
        assert h.events('waitless_utterance') == []
        await h.tap.on_response_end(response, h.clock.monotonic_ms())
        await settle(30)

    assert [e.transcript for e in h.events('waitless_utterance')] == [text]
    assert h.events('company_turn') == []
    assert not task.done() and received == []
    assert h.events('agent_tool_released') == []


@pytest.mark.parametrize('text', ['', ' \t\n '])
async def test_blank_user_final_is_written_without_releasing(repeat_call, text):
    h = repeat_call
    task, received = await held_repeat(h)

    await h.speech(text)

    turns = h.events('company_turn')
    assert [e.transcript for e in turns] == [text]
    assert turns[0].action == {'kind': 'speech', 'seq': 1}
    assert h.events('waitless_utterance') == []
    assert not task.done() and received == []
    assert h.events('agent_tool_released') == []


@pytest.mark.parametrize('prefix', ['', ' \t'])
async def test_user_event_final_is_not_written_or_released(repeat_call, prefix):
    h = repeat_call
    task, received = await held_repeat(h)

    await h.speech(prefix + '[Waitless event] {"event":"user_choice"}')

    assert h.events('company_turn') == []
    assert h.events('waitless_utterance') == []
    assert not task.done() and received == []
    assert h.events('agent_tool_released') == []


@pytest.mark.parametrize('role,response', [('USER', None), ('ASSISTANT', None), ('ASSISTANT', 1)])
async def test_real_final_is_written_and_releases(repeat_call, role, response):
    h = repeat_call
    task, received = await held_repeat(h)
    text = ' \tActual speech. '
    kind = 'company_turn' if role == 'USER' else 'waitless_utterance'

    await h.speech(text, role=role, response=response)
    if response is not None:
        assert h.events(kind) == []
        assert not task.done() and received == []
        await h.tap.on_response_end(response, h.clock.monotonic_ms())

    assert await asyncio.wait_for(task, 1) == {'ok': True}
    assert [e.transcript for e in h.events(kind)] == [text]
    assert received == [{'ok': True}]
    assert [e.action['reason'] for e in h.events('agent_tool_released')] == ['spoke']
    h.ledger()
