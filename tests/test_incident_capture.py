import gzip
import hashlib
import json
import os
from pathlib import Path

import httpx
import pytest

from tests.test_single_login import single
from tests.test_response_output import Split, sse, terminal
from windows.existing_adapter import ExistingAdapter
from windows.incident_capture import BodyBuffer, IncidentStore
from windows.runtime_diagnostics import diagnostics


ID='f8d5b6da-93fc-4d53-b6da-b4683e6e62ab'
ERROR={'type':'error','code':'original_provider_code','message':'original detailed provider message'}
HEADERS={'x-relay-request-id':ID,'authorization':'INBOUND_AUTH_MUST_NOT_BE_CAPTURED'}


def snapshots(adapter):
    return sorted(p for p in adapter.incidents.root.glob('*') if (p/'complete').exists())


def metadata(folder):return json.loads((folder/'metadata.json').read_text(encoding='utf-8'))


@pytest.mark.parametrize('stream',[True,False])
async def test_original_error_and_both_request_bodies_before_sanitization(config,state,tmp_path,stream):
    single(config,tmp_path);config.adapter.incident_capture_bodies=True
    config.adapter.cache_diagnostics_capture_inputs=True
    raw=sse([{'type':'response.output_text.delta','output_index':0,'content_index':0,'delta':'partial'},ERROR])
    sent=[]
    def handler(request):
        sent.append(request.content)
        return httpx.Response(200,stream=Split(raw),headers={'content-type':'text/event-stream','set-cookie':'RESPONSE_COOKIE_MUST_NOT_BE_CAPTURED'})
    adapter=ExistingAdapter(config,state,transport=httpx.MockTransport(handler))
    body=json.dumps({'model':'mock-codex','stream':stream,'input':[{'role':'user','content':[
        {'type':'input_text','text':'PRIVATE_PROMPT'}, {'type':'input_image','image_url':'data:image/png;base64,cGlj'}]},
        {'type':'function_call_output','call_id':'call_mock','output':'PRIVATE_TOOL_RESULT'}],
        'tools':[{'type':'function','name':'test','parameters':{'type':'object'}}]},indent=2).encode()
    try:
        with pytest.raises(ValueError,match='missing_terminal' if stream else 'upstream_error'):
            response=await adapter.open('POST','/v1/responses',body,HEADERS)
            try:await response.aread()
            finally:await response.aclose()
        folders=snapshots(adapter);assert len(folders)==1
        folder=folders[0];info=metadata(folder)
        assert info['request_id']==ID and info['request_attempts']==1
        assert gzip.decompress((folder/info['requests']['client']['file']).read_bytes())==body
        assert gzip.decompress((folder/info['requests']['upstream']['file']).read_bytes())==sent[0]
        assert json.loads(sent[0])['stream'] is True and json.loads(sent[0])['store'] is False
        assert gzip.decompress((folder/info['responses'][0]['files']['body']).read_bytes())==raw
        event=gzip.decompress((folder/info['error_events'][0]['file']).read_bytes())
        assert b'original_provider_code' in event and b'original detailed provider message' in event
        all_bytes=b''.join(gzip.decompress(p.read_bytes()) if p.suffix=='.gz' else p.read_bytes() for p in folder.iterdir())
        for secret in [b'INBOUND_AUTH_MUST_NOT_BE_CAPTURED',b'RESPONSE_COOKIE_MUST_NOT_BE_CAPTURED',b'SYNTHETIC_REFRESH_NEVER_USE',b'synthetic-workspace']:
            assert secret not in all_bytes
        public_log=adapter.diagnostics.path.read_bytes()
        assert b'PRIVATE_PROMPT' not in public_log and b'original_provider_code' not in public_log
        assert b'incident_captured' in public_log
        if os.name!='nt':
            assert folder.stat().st_mode&0o777==0o700
            assert all(p.stat().st_mode&0o777==0o600 for p in folder.iterdir())
    finally:await adapter.close()


@pytest.mark.parametrize('kind',['response.failed','response.incomplete'])
async def test_terminal_errors_are_retained_but_client_errors_stay_sanitized(config,state,tmp_path,kind):
    single(config,tmp_path);config.adapter.incident_capture_bodies=True
    event={'type':kind,'response':{'id':'resp_error','status':kind.split('.')[1],'output':[],
                                'error':{'code':'private_original','message':'private detail'}}}
    raw=sse([event])
    adapter=ExistingAdapter(config,state,transport=httpx.MockTransport(lambda r:httpx.Response(200,stream=Split(raw))))
    try:
        response=await adapter.open('POST','/v1/responses',b'{"model":"mock-codex","stream":false}',HEADERS)
        assert response.json()['error']['code']=='upstream_error'
        folder=snapshots(adapter)[0];info=metadata(folder)
        assert kind in info['error_events'][0]['type']
        assert b'private_original' in gzip.decompress((folder/info['responses'][0]['files']['body']).read_bytes())
    finally:await adapter.close()


async def test_normal_requests_do_not_create_incidents_and_do_not_evict_old_errors(config,state,tmp_path):
    single(config,tmp_path);config.adapter.incident_capture_bodies=True
    calls=[]
    def handler(request):
        calls.append(request)
        return httpx.Response(200,stream=Split(sse([ERROR] if len(calls)==1 else [terminal([])])))
    adapter=ExistingAdapter(config,state,transport=httpx.MockTransport(handler))
    try:
        with pytest.raises(ValueError):await adapter.open('POST','/v1/responses',b'{"model":"mock-codex"}',HEADERS)
        folder=snapshots(adapter)[0]
        for _ in range(25):
            r=await adapter.open('POST','/v1/responses',b'{"model":"mock-codex"}',HEADERS);await r.aclose()
        assert snapshots(adapter)==[folder]
        assert metadata(folder)['cache_record_id'] not in [r['id'] for r in adapter.cache_log.rows]
    finally:await adapter.close()


async def test_opt_in_and_transport_failure_with_partial_received_body(config,state,tmp_path):
    single(config,tmp_path)
    class Broken(httpx.AsyncByteStream):
        async def __aiter__(self):
            yield b'data: {"type":"response.created"}\n\n'
            raise httpx.RemoteProtocolError('EXCEPTION_SECRET_MUST_NOT_BE_CAPTURED')
    for enabled in [False,True]:
        config.adapter.incident_capture_bodies=enabled
        adapter=ExistingAdapter(config,state,transport=httpx.MockTransport(lambda r:httpx.Response(200,stream=Broken())))
        try:
            with pytest.raises(httpx.RemoteProtocolError):await adapter.open('POST','/v1/responses',b'{"model":"mock-codex"}',HEADERS)
            if enabled:
                folder=snapshots(adapter)[0];info=metadata(folder)
                assert info['exception']['error_code']=='protocol_error'
                assert 'EXCEPTION_SECRET' not in json.dumps(info)
                assert gzip.decompress((folder/info['responses'][0]['files']['body']).read_bytes()).startswith(b'data:')
            else:assert not snapshots(adapter)
        finally:await adapter.close()


async def test_http_error_retry_captures_each_response_body_without_changing_retry_policy(config,state,tmp_path,monkeypatch):
    single(config,tmp_path);config.adapter.incident_capture_bodies=True
    async def no_wait(*args):pass
    monkeypatch.setattr('windows.safe_retry.sleep',no_wait)
    calls=[]
    rejection={'error':{'type':'server_error','code':'server_is_overloaded','message':'original overload'}}
    def handler(request):
        calls.append(request)
        return httpx.Response(503,json=rejection) if len(calls)==1 else httpx.Response(200,stream=Split(sse([terminal([])])))
    adapter=ExistingAdapter(config,state,transport=httpx.MockTransport(handler))
    try:
        r=await adapter.open('POST','/v1/responses',b'{"model":"mock-codex"}',HEADERS);await r.aclose()
        folder=snapshots(adapter)[0];info=metadata(folder)
        assert len(calls)==2 and info['request_attempts']==2
        assert [r['http_status'] for r in info['responses']]==[503,200]
        raw=gzip.decompress((folder/info['responses'][0]['files']['body']).read_bytes())
        assert json.loads(raw)==rejection
    finally:await adapter.close()


async def test_disk_failure_does_not_mask_upstream_error_or_replay(config,state,tmp_path,monkeypatch):
    single(config,tmp_path);config.adapter.incident_capture_bodies=True;calls=[]
    def handler(request):
        calls.append(request);return httpx.Response(200,stream=Split(sse([ERROR])))
    adapter=ExistingAdapter(config,state,transport=httpx.MockTransport(handler))
    def fail(*args):raise OSError('DISK_SECRET')
    monkeypatch.setattr(adapter.incidents,'save',fail)
    try:
        with pytest.raises(ValueError,match='upstream_error'):
            await adapter.open('POST','/v1/responses',b'{"model":"mock-codex"}',HEADERS)
        assert len(calls)==1 and adapter.incidents.write_error
        assert b'incident_capture_failed' in adapter.diagnostics.path.read_bytes()
        assert b'DISK_SECRET' not in adapter.diagnostics.path.read_bytes()
    finally:await adapter.close()


def test_bounded_head_tail_and_total_hash():
    data=b'abcdefghijklmnopqrstuvwxyz';buffer=BodyBuffer(8)
    for i in range(0,len(data),3):buffer.add(data[i:i+3])
    assert bytes(buffer.head)==b'abcd' and b''.join(buffer.tail)==b'wxyz'
    assert buffer.info()=={'received_bytes':26,'captured_bytes':8,'truncated':True,
                           'missing_middle_bytes':18,'sha256':hashlib.sha256(data).hexdigest()}


async def test_retention_count_disk_cap_and_no_unrelated_deletion(config,state,monkeypatch):
    config.adapter.incident_capture_bodies=True
    store=IncidentStore(config,diagnostics(config,state))
    monkeypatch.setattr('windows.incident_capture.RECORD_LIMIT',2)
    store.root.mkdir(parents=True);unrelated=store.root/'keep-this';unrelated.mkdir();(unrelated/'note').write_text('keep')
    async def save():
        capture=store.begin(b'{"input":"sample"}',ID,None);capture.flag('synthetic_test');await capture.finish()
    for _ in range(4):await save()
    folders=sorted(p for p in store.root.iterdir() if (p/'complete').exists())
    assert len(folders)==2 and (unrelated/'note').read_text()=='keep'
    one_size=sum(p.stat().st_size for p in folders[-1].iterdir())
    monkeypatch.setattr('windows.incident_capture.DISK_LIMIT',one_size+10)
    await save()
    assert len([p for p in store.root.iterdir() if (p/'complete').exists()])==1
    assert (unrelated/'note').read_text()=='keep'


async def test_truncation_is_explicit_and_error_event_is_kept_separately(config,state,monkeypatch):
    config.adapter.incident_capture_bodies=True
    monkeypatch.setattr('windows.incident_capture.RESPONSE_LIMIT',8)
    store=IncidentStore(config,diagnostics(config,state));capture=store.begin(b'{}',ID,None)
    response=httpx.Response(200,stream=Split(sse([ERROR])))
    await capture.response(response)
    raw=b''.join([chunk async for chunk in response.stream])
    await capture.finish()
    folder=next(p for p in store.root.iterdir() if (p/'complete').exists());info=metadata(folder)
    item=info['responses'][0]
    assert item['truncated'] and item['captured_bytes']==8 and item['received_bytes']==len(raw)
    assert set(item['files'])=={'head','tail'}
    assert b'original_provider_code' in gzip.decompress((folder/info['error_events'][0]['file']).read_bytes())
