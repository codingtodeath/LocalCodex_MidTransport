import json
import asyncio
import time
from decimal import Decimal

import httpx
import pytest

from codex_relay import api_proxy, key_wallet
from codex_relay.pricing import insert_price_rule
from windows.bridge import create_app
from windows.existing_adapter import ExistingAdapter
from test_server_regression import service
from tests.test_key_wallet import key_request
from tests.test_single_login import single
from tests.test_safe_retry import BODY, SSE, success, fast_retry


@pytest.mark.parametrize('stream', [True, False])
async def test_three_windows_attempts_are_one_gateway_request_and_charge(service, config, state, tmp_path, fast_retry, monkeypatch, stream):
    single(config, tmp_path); calls = []
    def upstream(request):
        calls.append(request)
        if len(calls) < 3: raise httpx.ConnectError('synthetic', request=request)
        return success()
    adapter = ExistingAdapter(config, state, transport=httpx.MockTransport(upstream))
    app = create_app(config, state, adapter)
    monkeypatch.setattr(api_proxy, 'make_client', lambda _: httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), trust_env=False))
    _, key, ident, attempt, started = key_request(service, stream=stream)
    insert_price_rule(service.db, {'price_version':'test', 'model':'mock-codex', 'currency':'USD',
        'effective_from':'2020-01-01T00:00:00Z', 'prices_per_million':{'input':'2','cached_input':'.1','output':'10'}})
    service.reload_prices()
    service.gate.try_acquire(key.key_id)
    try:
        handler = api_proxy._handle_stream if stream else api_proxy._handle_nonstream
        response = await handler(service, url='http://bridge/v1/responses',
            headers={'authorization':'Bearer ' + config.bridge.auth_secret_file.read_text(), 'content-type':'application/json',
                     'x-relay-request-id':ident}, body=json.dumps(dict(BODY, stream=stream)).encode(),
            request_id=ident, attempt_id=attempt, client_key=key, requested_model='mock-codex',
            started_at=started, started_monotonic=time.monotonic())
        assert response.status_code == 200
        if stream:
            assert b''.join([chunk async for chunk in response.body_iterator]) == SSE
            await response.background()
        assert len(calls) == 3 and state.active_requests == 0 and service.gate.global_active == 0
        record = service.db.query_one('SELECT usage_status,total_tokens FROM requests WHERE request_id=?', (ident,))
        assert record['usage_status'] == 'reported' and record['total_tokens'] == 11
        assert service.db.query_one("SELECT COUNT(*) FROM key_balance_entries WHERE kind='charge'")[0] == 1
        assert Decimal(service.db.query_one('SELECT balance_usd FROM client_keys WHERE key_id=?', (key.key_id,))[0]) == Decimal('.99997')
        assert len(json.loads(adapter.cache_log.path.read_text())['requests']) == 1
    finally:
        await adapter.close()


@pytest.mark.parametrize('status,code', [(503,'upstream_connect_failed'), (503,'upstream_unavailable'),
    (503,'upstream_overloaded'), (402,'upstream_quota_exhausted')])
def test_gateway_preserves_specific_failure_without_readiness_mislabel(status, code):
    error = api_proxy._map_upstream_error(status, '60', code)
    assert error.status_code == status and error.code == code
    if status == 503: assert error.retry_after == 60


async def test_key_stop_cancels_windows_retry_and_frees_both_slots(service, config, state, tmp_path, monkeypatch):
    from codex_relay import auth
    from codex_relay.errors import RelayError
    single(config, tmp_path); calls = []; waiting = asyncio.Event(); cancelled = asyncio.Event()
    def upstream(request):
        calls.append(request); raise httpx.ConnectError('synthetic', request=request)
    async def sleep(delay):
        waiting.set()
        try: await asyncio.sleep(10)
        finally: cancelled.set()
    monkeypatch.setattr('windows.safe_retry.sleep', sleep)
    adapter = ExistingAdapter(config, state, transport=httpx.MockTransport(upstream))
    app = create_app(config, state, adapter)
    monkeypatch.setattr(api_proxy, 'make_client', lambda _: httpx.AsyncClient(transport=httpx.ASGITransport(app=app), trust_env=False))
    _, key, ident, attempt, started = key_request(service)
    service.gate.try_acquire(key.key_id)
    pending = asyncio.create_task(api_proxy._handle_stream(service, url='http://bridge/v1/responses',
        headers={'authorization':'Bearer ' + config.bridge.auth_secret_file.read_text(), 'content-type':'application/json'},
        body=json.dumps(BODY).encode(), request_id=ident, attempt_id=attempt, client_key=key,
        requested_model='mock-codex', started_at=started, started_monotonic=time.monotonic()))
    try:
        await asyncio.wait_for(waiting.wait(), 1)
        auth.revoke_client_key(service.db, key.key_id)
        with pytest.raises(RelayError) as exc: await asyncio.wait_for(pending, 1.5)
        assert exc.value.status_code == 401 and cancelled.is_set() and len(calls) == 1
        assert service.gate.global_active == state.active_requests == 0
        assert service.db.query_one("SELECT COUNT(*) FROM key_balance_entries WHERE kind='charge'")[0] == 0
    finally:
        if not pending.done(): pending.cancel(); await asyncio.gather(pending, return_exceptions=True)
        await adapter.close()
