"""Revocation is a shared contract, including every test issuer."""
import ast
import inspect
from pathlib import Path

import pytest

from waitless.interfaces import HandoffTokenIssuer, LineTokenIssuer
from waitless.call.handoff import HandoffTokens, handoff_endpoint
from tests.a_support import FakeHandoffTokens, FakeLineTokens
from tests.e_support.stubs import StubHandoffTokens, StubLineTokens, SimpleClock
from tests.call_engine._support import SimpleHandoffTokens, SimpleLineTokens, AsgiWs, HANDOFF_ROUTE, run_endpoint
from tests.call_engine.test_line_auth import make_services
from tests.call_engine.conftest import settings, store, trace, engine, line_number


def handoffs(clock):
    return [HandoffTokens(clock, ttl_s=120), FakeHandoffTokens(),
            SimpleHandoffTokens(clock, 120), StubHandoffTokens(clock)]


def test_protocol_covers_live_token_calls():
    from waitless.call import live
    tree = ast.parse(inspect.getsource(live))
    for attr, protocol in [('line_tokens', LineTokenIssuer), ('handoff_tokens', HandoffTokenIssuer)]:
        methods = {node.func.attr for node in ast.walk(tree) if isinstance(node, ast.Call)
                   and isinstance(node.func, ast.Attribute) and isinstance(node.func.value, ast.Attribute)
                   and node.func.value.attr == attr}
        assert methods <= set(dir(protocol))


def test_handoff_revocation_contract_and_generations(fake_clock):
    for issuer in handoffs(fake_clock):
        assert isinstance(issuer, HandoffTokenIssuer)
        old, _ = issuer.issue('a', 'h')
        other, _ = issuer.issue('b', 'h')
        issuer.revoke('unknown')
        issuer.revoke('a')
        issuer.revoke('a')
        new, _ = issuer.issue('a', 'h')
        assert old != new
        assert issuer.consume('h', old) is None
        assert issuer.consume('h', new) == 'a'
        assert issuer.consume('h', new) is None
        assert issuer.consume('h', other) == 'b'
        if isinstance(issuer, StubHandoffTokens):
            assert len(issuer.issued) == 3  # Audit survives live deletion.


def test_line_revocation_and_no_counter_reuse():
    for issuer in [FakeLineTokens(), SimpleLineTokens(), StubLineTokens()]:
        assert isinstance(issuer, LineTokenIssuer)
        a = issuer.issue('a')
        b = issuer.issue('b')
        issuer.revoke('a')
        issuer.revoke('a')
        issuer.revoke('unknown')
        c = issuer.issue('c')
        assert c not in (a, b)
        assert issuer.verify(a) is None
        if isinstance(issuer, StubLineTokens):
            assert issuer.verify(c) is None
        else:
            assert issuer.verify(b) == 'b'
            assert issuer.verify(c) == 'c'


async def test_revoked_handoff_is_4401_without_attach(settings, store, trace, fake_clock):
    services, _ = make_services(settings, store, trace, fake_clock, {})
    for issuer in handoffs(fake_clock):
        services.handoff_tokens = issuer
        token, _ = issuer.issue('a', 'h')
        issuer.revoke('a')
        ws = AsgiWs(path=HANDOFF_ROUTE, query={'token': token}, services=services)
        await run_endpoint(handoff_endpoint(ws.websocket, 'h'), ws)
        assert ws.close_code == 4401
        assert services.calls.lookups == []
