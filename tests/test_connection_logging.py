import asyncio
import json
import time
import uuid

import httpx
import pytest

from codex_relay import auth, worker_state, api_proxy
from codex_relay.app import create_app
from codex_relay.connection_log import ConnectionObserver, error_kind
from tests.test_server_regression import service, heartbeat, stream_call


def records(service):
    return [json.loads(line) for line in service.connection_log.path.read_text().splitlines()]


def ready(service):
    from datetime import datetime, timezone
    now=datetime.now(timezone.utc);ident=str(uuid.uuid4());heartbeat(service,ident,now)
    worker_state.record_probe(service.db,service.config.worker_id,ok=True,instance_id=ident,now=now)


async def test_rejected_auth_has_correlated_id_and_never_logs_secrets(service):
    app=create_app(service.config,services=service)
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app),base_url='http://server') as c:
        r=await c.post('/v1/responses?secret=SECRET_QUERY',json={'input':'SECRET_INPUT'},headers={
            'Authorization':'Bearer gw_SECRET_KEY','session-id':'SECRET_SESSION','X-Request-ID':'SECRET_FORGED','X-Relay-Edge-Id':'a'*32})
    assert r.status_code==401 and r.headers['x-relay-error-code']=='invalid_api_key'
    ident=str(uuid.UUID(r.headers['x-request-id']))
    events=records(service)
    assert events[0]['request_id']==events[-1]['request_id']==ident
    assert events[-1]['event']=='request_rejected' and events[-1]['error_code']=='invalid_api_key'
    assert events[-1]['session_fp']!='SECRET_SESSION' and events[-1]['edge_id']=='a'*32
    assert 'SECRET' not in service.connection_log.path.read_text()


async def test_concurrency_rejection_contains_active_counts(service):
    ready(service);token,key=auth.create_client_key(service.db,service.pepper,'fixture')
    assert service.gate.try_acquire(key.key_id) is None
    app=create_app(service.config,services=service)
    try:
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app),base_url='http://server') as c:
            r=await c.post('/v1/responses',json={'model':'mock-codex'},headers={'Authorization':'Bearer '+token})
        assert r.status_code==429
        event=records(service)[-1]
        assert event['error_code']=='gateway_concurrency_limit' and event['client_active']==1 and event['client_limit']==1
        assert event['global_active']==1 and event['adapter_ready']
        assert token not in service.connection_log.path.read_text()
    finally:service.gate.release(key.key_id)


async def test_unready_rejection_includes_worker_facts(service):
    token,_=auth.create_client_key(service.db,service.pepper,'fixture')
    app=create_app(service.config,services=service)
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app),base_url='http://server') as c:
        r=await c.post('/v1/responses',json={'model':'mock-codex'},headers={'Authorization':'Bearer '+token})
    assert r.status_code==503
    event=records(service)[-1]
    assert not event['heartbeat_fresh'] and event['worker_state'] in {'unknown','offline','degraded'}


async def test_stream_cancelled_has_last_packet_metrics_and_releases_slot(service,monkeypatch):
    response,source,ident=await stream_call(service,monkeypatch,10)
    await response.body_iterator.__anext__()
    task=asyncio.create_task(response.body_iterator.__anext__())
    await asyncio.sleep(.01);task.cancel()
    with pytest.raises(asyncio.CancelledError):await task
    event=records(service)[-1]
    assert event['event']=='stream_finished' and event['outcome']=='cancelled_cause_unknown'
    assert event['request_id']==ident and not event['terminal_seen']
    assert service.gate.global_active==0


async def test_observer_records_disconnect_without_buffering_or_changing_error(service):
    async def app(scope,receive,send):
        await send({'type':'http.response.start','status':200,'headers':[]})
        await send({'type':'http.response.body','body':b'SECRET_STREAM','more_body':True})
        assert (await receive())['type']=='http.disconnect'
        raise asyncio.CancelledError()
    observer=ConnectionObserver(app,service);sent=[]
    async def receive():return {'type':'http.disconnect'}
    async def send(message):sent.append(message)
    with pytest.raises(asyncio.CancelledError):
        await observer({'type':'http','path':'/v1/responses','method':'POST','headers':[],'client':('127.0.0.1',1)},receive,send)
    assert sent[1]['body']==b'SECRET_STREAM'
    event=records(service)[-1]
    assert event['disconnect_observed'] and not event['response_complete'] and event['bytes_sent']==13
    assert event['outcome']=='cancelled_cause_unknown'
    assert 'SECRET' not in service.connection_log.path.read_text()


async def test_log_write_failure_does_not_fail_requests(service,monkeypatch):
    app=create_app(service.config,services=service)
    with monkeypatch.context() as m:
        m.setattr('codex_relay.connection_log.os.open',lambda *a,**k:(_ for _ in ()).throw(PermissionError('SECRET')))
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app),base_url='http://server') as c:r=await c.get('/v1/models')
    assert r.status_code==401 and service.connection_log.write_error


def test_timeout_classes_remain_distinct():
    assert error_kind(httpx.ReadTimeout('SECRET'))=='worker_idle_timeout'
    assert error_kind(TimeoutError())=='request_deadline_exceeded'
    assert error_kind(httpx.ConnectTimeout('SECRET'))=='worker_connect_timeout'


@pytest.mark.parametrize('idle',[True,False])
async def test_stream_distinguishes_idle_timeout_and_total_deadline(service,monkeypatch,idle):
    response,source,ident=await stream_call(service,monkeypatch,10)
    if idle:
        async def interrupted(self):
            yield b'data: {}\n\n'
            raise httpx.ReadTimeout('SECRET')
        monkeypatch.setattr(type(source),'__aiter__',interrupted)
    with pytest.raises(httpx.ReadTimeout if idle else TimeoutError):
        async for _ in response.body_iterator:pass
    event=records(service)[-1]
    assert event['outcome']==('worker_idle_timeout' if idle else 'request_deadline_exceeded')
    assert event['bytes_received']>0 and event['last_byte_ms']>=event['first_byte_ms']
    assert service.gate.global_active==0 and source.closed


async def test_one_request_id_spans_gateway_bridge_cache_and_upstream(config,state,tmp_path,service,monkeypatch):
    from windows.bridge import create_app as bridge_app
    from windows.existing_adapter import ExistingAdapter
    from tests.test_single_login import single
    single(config,tmp_path)
    body=b'event: response.completed\ndata: {"type":"response.completed","response":{"id":"fixture","model":"mock-codex","status":"completed","output":[],"usage":{"input_tokens":5,"output_tokens":1,"input_tokens_details":{"cached_tokens":0}}}}\n\n'
    class ByteStream(httpx.AsyncByteStream):
        async def __aiter__(self):
            yield body
    seen=[]
    def provider(request):
        seen.append(request)
        return httpx.Response(200,stream=ByteStream(),headers={'content-type':'text/event-stream'})
    adapter=ExistingAdapter(config,state,transport=httpx.MockTransport(provider))
    bridge=bridge_app(config,state,adapter)
    service.worker_key=config.bridge.auth_secret_file.read_text()
    ready(service);token,_=auth.create_client_key(service.db,service.pepper,'fixture')
    monkeypatch.setattr(api_proxy,'make_client',lambda _:httpx.AsyncClient(transport=httpx.ASGITransport(app=bridge),base_url='http://bridge',trust_env=False))
    app=create_app(service.config,services=service)
    try:
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app),base_url='http://server') as c:
            r=await c.post('/v1/responses',json={'model':'mock-codex','stream':True,'input':'SECRET_INPUT'},headers={'Authorization':'Bearer '+token,'session-id':'SECRET_SESSION'})
        assert r.status_code==200 and r.content==body
        ident=r.headers['x-request-id']
        assert service.db.query_one('select request_id from requests')['request_id']==ident
        worker_rows=[json.loads(line) for line in adapter.diagnostics.path.read_text().splitlines()]
        assert any(x['event']=='inference_stream_finished' and x.get('request_id')==ident and x['terminal_seen'] for x in worker_rows)
        assert any(x['event']=='bridge_request_finished' and x['request_id']==ident for x in worker_rows)
        cached=json.loads(adapter.cache_log.path.read_text())['requests'][-1]
        assert cached['relay_request_id']==ident
        event=[x for x in records(service) if x['event']=='stream_finished'][-1]
        assert event['request_id']==ident and event['terminal_seen'] and event['bytes_received']==len(body)
        assert 'SECRET' not in service.connection_log.path.read_text() and 'SECRET' not in adapter.diagnostics.path.read_text()
    finally:await adapter.close()
