"""Verify request ownership, replay boundaries, deadlines and cancellation."""
import asyncio
import json
import ssl
import time
from datetime import datetime, timedelta, timezone
from email.utils import format_datetime
from types import SimpleNamespace

import httpx
import pytest

from windows.bridge import create_app, open_until_disconnect
from windows.existing_adapter import ExistingAdapter
from windows.safe_retry import retry_after
from tests.test_single_login import single
from tests.test_runtime_diagnostics import rows

BODY = {'model': 'mock-codex', 'stream': True, 'input': [
    {'role': 'user', 'content': [{'type': 'input_text', 'text': 'SECRET_PROMPT'},
        {'type': 'input_image', 'image_url': 'data:image/png;base64,U1lOVEhFVElD'}]},
    {'type': 'function_call_output', 'call_id': 'call_mock', 'output': 'SECRET_TOOL_RESULT'}],
    'tools': [{'type': 'function', 'name': 'mock', 'parameters': {'type': 'object'}}],
    'prompt_cache_key': 'synthetic-cache-key'}
HEADERS = {'x-relay-request-id': 'c7abef53-59dd-4a69-b1a6-5e5b616825e1',
           'session-id': 'synthetic-session', 'x-codex-turn-state': 'synthetic-turn'}
TERMINAL = {'id': 'resp_mock', 'model': 'mock-codex', 'status': 'completed', 'output': [],
            'usage': {'input_tokens': 10, 'input_tokens_details': {'cached_tokens': 0}, 'output_tokens': 1, 'total_tokens': 11}}
SSE = ('event: response.completed\ndata: ' + json.dumps({'type': 'response.completed', 'response': TERMINAL}) + '\n\n').encode()


@pytest.fixture
def fast_retry(monkeypatch):
    waits = []
    async def sleep(seconds):
        waits.append(seconds)
        await asyncio.sleep(0)
    monkeypatch.setattr('windows.safe_retry.sleep', sleep)
    monkeypatch.setattr('windows.safe_retry.random.uniform', lambda *args: .1)
    return waits


def success():
    return httpx.Response(200, stream=httpx.ByteStream(SSE), headers={'content-type': 'text/event-stream'})


async def test_connection_retry_preserves_body_images_tools_headers_and_one_record(config, state, tmp_path, fast_retry):
    auth = single(config, tmp_path); original = auth.read_bytes(); calls = []
    def handler(request):
        calls.append(request)
        if len(calls) < 3:
            raise httpx.ConnectError('SECRET_URL', request=request)
        return success()
    adapter = ExistingAdapter(config, state, transport=httpx.MockTransport(handler))
    try:
        r = await adapter.open('POST', '/v1/responses', json.dumps(BODY).encode(), HEADERS)
        assert await r.aread() == SSE
        await r.aclose()
        assert fast_retry == [1.1, 2.1] and len(calls) == 3
        assert all(c.method == 'POST' and c.url.path.endswith('/responses') for c in calls)
        assert all(c.content == calls[0].content and c.headers == calls[0].headers for c in calls)
        sent = json.loads(calls[0].content)
        assert sent['input'] == BODY['input'] and sent['tools'] == BODY['tools']
        assert calls[0].headers['session-id'] == HEADERS['session-id']
        assert auth.read_bytes() == original
        events = rows(adapter.diagnostics)
        attempts = [e for e in events if e['event'] == 'inference_attempt_started']
        assert [e['attempt_no'] for e in attempts] == [1, 2, 3]
        assert len({e['cache_record_id'] for e in attempts}) == 1
        assert all(e['request_id'] == HEADERS['x-relay-request-id'] for e in attempts)
        assert len(json.loads(adapter.cache_log.path.read_text())['requests']) == 1
        assert 'SECRET' not in adapter.diagnostics.path.read_text()
    finally:
        await adapter.close()


@pytest.mark.parametrize('kind', [httpx.ReadTimeout, httpx.WriteTimeout, httpx.ReadError, httpx.RemoteProtocolError])
async def test_ambiguous_transport_failure_never_replays(config, state, tmp_path, fast_retry, kind):
    single(config, tmp_path); calls = []
    async def handler(request):
        calls.append(request)
        await request.extensions['trace']('http11.send_request_headers.started', {})
        raise kind('SECRET', request=request)
    adapter = ExistingAdapter(config, state, transport=httpx.MockTransport(handler))
    try:
        with pytest.raises(kind): await adapter.open('POST', '/v1/responses', json.dumps(BODY), HEADERS)
        assert len(calls) == 1 and not fast_retry
        failed = [r for r in rows(adapter.diagnostics) if r['event'] == 'inference_attempt_failed'][0]
        assert failed['request_sent'] and not failed['retryable']
    finally: await adapter.close()


@pytest.mark.parametrize('sent', [False, True])
async def test_connect_error_certificate_or_after_headers_never_replays(config, state, tmp_path, fast_retry, sent):
    single(config, tmp_path); calls = []
    async def handler(request):
        calls.append(request)
        if sent: await request.extensions['trace']('http2.send_request_headers.started', {})
        error = httpx.ConnectError('SECRET', request=request)
        if not sent: error.__cause__ = ssl.SSLCertVerificationError('SECRET_CERT')
        raise error
    adapter = ExistingAdapter(config, state, transport=httpx.MockTransport(handler))
    try:
        with pytest.raises(httpx.ConnectError): await adapter.open('POST', '/v1/responses', json.dumps(BODY))
        assert len(calls) == 1 and not fast_retry
    finally: await adapter.close()


@pytest.mark.parametrize('status,kind,code', [(429, 'rate_limit_error', 'rate_limit_exceeded'),
    (503, 'server_error', 'server_is_overloaded'), (503, 'rate_limit_error', 'slow_down')])
async def test_only_explicit_temporary_rejections_retry(config, state, tmp_path, fast_retry, status, kind, code):
    single(config, tmp_path); calls = []
    def handler(request):
        calls.append(request)
        return httpx.Response(status, json={'error': {'type': kind, 'code': code}}) if len(calls) < 3 else success()
    adapter = ExistingAdapter(config, state, transport=httpx.MockTransport(handler))
    try:
        r = await adapter.open('POST', '/v1/responses', json.dumps(BODY))
        assert await r.aread() == SSE
        assert len(calls) == 3 and fast_retry == [1.1, 2.1]
        await r.aclose()
    finally: await adapter.close()


@pytest.mark.parametrize('status,payload', [(400, {}), (401, {}), (403, {}), (402, {}),
    (429, {'error': {'type': 'rate_limit_error', 'code': 'insufficient_quota'}}),
    (429, {'error': {'code': 'rate_limit_exceeded'}}), (500, {}), (503, {}),
    (503, {'error': {'type': 'server_error', 'code': 'server_is_overloaded'}, 'usage': {}}),
    (429, {'error': {'code': {}, 'type': []}})])
async def test_quota_auth_unstructured_errors_never_retry(config, state, tmp_path, fast_retry, status, payload):
    single(config, tmp_path); calls = []
    def handler(request):
        calls.append(request); return httpx.Response(status, json=payload)
    adapter = ExistingAdapter(config, state, transport=httpx.MockTransport(handler))
    try:
        r = await adapter.open('POST', '/v1/responses', json.dumps(BODY))
        assert r.status_code == status and len(calls) == 1 and not fast_retry
        await r.aclose()
    finally: await adapter.close()


@pytest.mark.parametrize('hint,expected_calls,expected_waits', [('7', 3, [7.1, 7.1]), ('60', 1, [])])
async def test_retry_after_is_minimum_and_cannot_exceed_budget(config, state, tmp_path, fast_retry, hint, expected_calls, expected_waits):
    single(config, tmp_path); calls = []
    def handler(request):
        calls.append(request)
        return httpx.Response(429, headers={'retry-after': hint}, json={'error': {'type': 'rate_limit_error', 'code': 'rate_limit_exceeded'}})
    adapter = ExistingAdapter(config, state, transport=httpx.MockTransport(handler))
    try:
        r = await adapter.open('POST', '/v1/responses', json.dumps(BODY))
        assert r.status_code == 429 and r.headers['retry-after'] == hint
        assert len(calls) == expected_calls and fast_retry == expected_waits
        await r.aclose()
    finally: await adapter.close()


def test_retry_after_date_and_invalid_values():
    hint = format_datetime(datetime.now(timezone.utc) + timedelta(seconds=8), usegmt=True)
    assert 6 < retry_after(hint) <= 8
    for hint in (None, '', '-1', 'NaN', 'infinity', 'x' * 129): assert retry_after(hint) is None


async def test_open_deadline_is_bounded_and_does_not_replay(config, state, tmp_path, fast_retry):
    single(config, tmp_path); config.adapter.safe_retry_budget_seconds = .03; calls = []
    async def handler(request):
        calls.append(request)
        await request.extensions['trace']('http11.send_request_headers.started', {})
        await asyncio.sleep(5)
        return success()
    adapter = ExistingAdapter(config, state, transport=httpx.MockTransport(handler))
    try:
        started = time.monotonic()
        with pytest.raises(httpx.ReadTimeout): await adapter.open('POST', '/v1/responses', json.dumps(BODY))
        assert time.monotonic() - started < .5 and len(calls) == 1 and not fast_retry
    finally: await adapter.close()


async def test_accepted_stream_failure_cannot_trigger_retry(config, state, tmp_path, fast_retry):
    single(config, tmp_path); calls = []
    class Broken(httpx.AsyncByteStream):
        async def __aiter__(self):
            yield b'data: {"type":"response.created"}\n\n'
            raise httpx.ReadError('SECRET_STREAM')
    def handler(request):
        calls.append(request); return httpx.Response(200, stream=Broken(), headers={'content-type': 'text/event-stream'})
    adapter = ExistingAdapter(config, state, transport=httpx.MockTransport(handler))
    try:
        r = await adapter.open('POST', '/v1/responses', json.dumps(BODY))
        with pytest.raises(httpx.ReadError): await r.aread()
        await r.aclose()
        assert len(calls) == 1 and not fast_retry
    finally: await adapter.close()


async def test_disconnect_cancels_pending_retry(config, state, tmp_path, monkeypatch):
    single(config, tmp_path); calls = []; waiting = asyncio.Event(); cancelled = asyncio.Event()
    def handler(request):
        calls.append(request); raise httpx.ConnectError('SECRET', request=request)
    async def sleep(delay):
        waiting.set()
        try: await asyncio.sleep(10)
        finally: cancelled.set()
    monkeypatch.setattr('windows.safe_retry.sleep', sleep)
    adapter = ExistingAdapter(config, state, transport=httpx.MockTransport(handler))
    async def disconnected(): return waiting.is_set()
    try:
        request = SimpleNamespace(headers=HEADERS, is_disconnected=disconnected)
        with pytest.raises(asyncio.CancelledError):
            await asyncio.wait_for(open_until_disconnect(adapter, request, json.dumps(BODY).encode()), 1)
        assert cancelled.is_set() and len(calls) == 1
    finally: await adapter.close()


async def test_network_failure_does_not_make_local_adapter_unready(config, state, tmp_path, fast_retry):
    single(config, tmp_path); calls = []
    def handler(request):
        calls.append(request); raise httpx.ConnectError('SECRET', request=request)
    adapter = ExistingAdapter(config, state, transport=httpx.MockTransport(handler)); app = create_app(config, state, adapter)
    try:
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url='http://bridge',
                headers={'Authorization': 'Bearer ' + config.bridge.auth_secret_file.read_text()}) as client:
            assert (await client.get('/internal/v1/health')).json()['local']['adapter_ready']
            r = await client.post('/v1/responses', json=BODY)
            assert r.status_code == 503 and r.json()['error']['code'] == 'upstream_connect_failed'
            assert (await client.get('/internal/v1/health')).json()['local']['adapter_ready']
            assert len(calls) == 3 and state.active_requests == 0
    finally: await adapter.close()


async def test_proxy_tunnel_setup_failure_is_not_inference_submission(config, state, tmp_path, fast_retry):
    single(config, tmp_path); calls = []
    async def handler(request):
        calls.append(request)
        await request.extensions['trace']('http11.send_request_headers.started', {'request': SimpleNamespace(method=b'CONNECT')})
        await request.extensions['trace']('http11.send_request_body.started', {'request': SimpleNamespace(method=b'CONNECT')})
        if len(calls) < 3: raise httpx.ConnectError('SECRET_TUNNEL', request=request)
        await request.extensions['trace']('http11.send_request_headers.started', {'request': SimpleNamespace(method=b'POST')})
        return success()
    adapter = ExistingAdapter(config, state, transport=httpx.MockTransport(handler))
    try:
        r = await adapter.open('POST', '/v1/responses', json.dumps(BODY))
        assert await r.aread() == SSE and len(calls) == 3
        await r.aclose()
    finally: await adapter.close()
