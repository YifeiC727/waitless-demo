"""F2: provider FINAL semantic views, original audit text preserved."""
import pytest
from fakes import FakeJev
from call_engine._support import settle
from lane_v528a.test_keypad_policy import (live528, script, agent_call, settings, spec,
    engine, compiled, line_number, store, trace)
from lane_D.test_v528d_split import row

@pytest.mark.parametrize('text,semantic,reason', [
    ('[name]', '', 'speaker_placeholder_transcript'),
    ('[agent name]', '', 'speaker_placeholder_transcript'),
    ('hello, this is [name]. How may I help?', '', 'speaker_placeholder_transcript'),
    ('Real words [account number] still real', '', 'speaker_placeholder_transcript'),
    ('[ Date ]', '', 'speaker_placeholder_transcript'),
    ('[time]', '', 'speaker_placeholder_transcript'),
    ('[amount]', '', 'speaker_placeholder_transcript'),
    ('[address]', '', 'speaker_placeholder_transcript'),
    ('[_ahhh]', '', 'speaker_empty_transcript'),
    ('  [_ahhh]  ', '', 'speaker_empty_transcript'),
    ('[_ahhh] [_mmm]', '', 'speaker_empty_transcript'),
    ('Real [_ahhh] speech', 'Real  speech', None),
    ('[AB12]', '[AB12]', None),
    ('[]', '[]', None),
    ('[keys pressed: *]', '[keys pressed: *]', None),
    ('[_AHHH]', '[_AHHH]', None),
    ('[agent  name]', '[agent  name]', None),
    ('[_ahhh ]', '[_ahhh ]', None),
    ('   ', '', 'speaker_empty_transcript'),
])
async def test_f2_company_final_semantic_filter_and_raw_trace(live528, text, semantic, reason):
    h = live528; k = h.call._keypad
    k.deps.jev = FakeJev(default=script(key='none'))
    await h.sonic.emit_user_text(text); await settle(250)
    assert h.events('company_turn')[-1].transcript == text
    if reason:
        assert not k.deps.jev.calls
        d = h.events('decision')[-1]
        assert d.policy_decision['reason_code'] == reason
        assert d.policy_decision['no_jev'] is True
        assert k.policy_state.uncertain_run == k.policy_state.unavailable_run == 0
        assert h.call._media_gate.output_state == 'closed'
        assert not k._state().transcript.lines
    else:
        assert len(k.deps.jev.calls) == 2
        assert all(call.state.transcript.lines[-1].text == semantic for call in k.deps.jev.calls)

@pytest.mark.parametrize('text', ['Hello [name] real speech', '[_ahhh]'])
async def test_f2_empty_semantics_never_reenter_next_windows_or_backfill(live528, text):
    h = live528; k = h.call._keypad
    k.deps.jev = FakeJev(default=script(key='none'))
    await h.sonic.emit_user_text(text); await settle(200)
    await h.sonic.emit_user_text('Next company turn'); await settle(250)
    assert len(k.deps.jev.calls) == 2
    assert k._last_decided_seq == 2
    for call in k.deps.jev.calls:
        assert [line.text for line in call.state.transcript.lines] == ['Next company turn']

async def test_f2_waitless_placeholder_and_key_row_are_preserved(live528):
    h = live528; k = h.call._keypad
    k.deps.jev = FakeJev(default=script(key='*'))
    await h.sonic.emit_user_text('Menu'); await settle(250)
    row(h, 'waitless_utterance', '[name] [_ahhh]')
    k.deps.jev = FakeJev(default=script(key='none'))
    await h.sonic.emit_user_text('Company turn'); await settle(250)
    for call in k.deps.jev.calls:
        lines = [(line.speaker, line.text) for line in call.state.transcript.lines]
        assert ('waitless', '[name] [_ahhh]') in lines
        assert ('waitless', '[keys pressed: *]') in lines

async def test_f2_placeholder_then_hangup_during_failed_pair_reaping_keeps_http_owned(agent_call):
    import asyncio
    from types import SimpleNamespace
    from waitless.call.live import UNRECLAIMED
    from lane_D.test_u119_speaker import response_model, reply
    from lane_D.test_v528d_requests import boot_unique
    h = await boot_unique(agent_call)
    tasks = {}
    entered = asyncio.Event(); fail_main = asyncio.Event(); cancelling = asyncio.Event(); late = asyncio.Event()
    async def ask(state, questions):
        side = 'transfer' if set(questions) == {'transfer'} else 'main'
        tasks[side] = asyncio.current_task()
        if len(tasks) == 2: entered.set()
        if side == 'main':
            await fail_main.wait()
            raise RuntimeError('offline main')
        try:
            await asyncio.Event().wait()
        except asyncio.CancelledError:
            cancelling.set()
            await late.wait()
            return response_model({'transfer': reply()['transfer']})
    h.call._keypad.deps.jev = SimpleNamespace(ask=ask)
    await h.sonic.emit_user_text('Company words')
    await entered.wait(); fail_main.set(); await cancelling.wait()
    try:
        await h.sonic.emit_user_text('[name]')
        assert not tasks['transfer'].done() and tasks['transfer'] not in UNRECLAIMED
        await h.call.hangup('test_cleanup')
        assert tasks['transfer'].done() or tasks['transfer'] in UNRECLAIMED
        assert h.call._media_gate.output_state == 'ended'
    finally:
        late.set()
        await asyncio.gather(*tasks.values(), return_exceptions=True)
        await settle(100)
    assert tasks['transfer'] not in UNRECLAIMED
    assert not h.events('handoff_ready')
