import io
import json
import uuid
from datetime import datetime, timezone
import pytest
from tools.collect_connection_diagnostics import read_events, build_report
from tools import client_connection_probe as probe_tool


def test_collector_projects_fields_and_groups_ids_without_input(tmp_path):
    ident=str(uuid.uuid4());now=datetime.now(timezone.utc)
    path=tmp_path/'events.jsonl'
    path.write_text(json.dumps({'at':now.isoformat(),'event':'request_rejected','request_id':ident,
        'error_code':'invalid_api_key','path':'/v1/not_a_secret','input':'SECRET','authorization':'SECRET'})+'\nnot json\n',encoding='utf-8')
    rows,truncated=read_events([path],now)
    assert not truncated and len(rows)==1 and 'SECRET' not in json.dumps(rows)
    assert rows[0]['path']=='/other'
    remote={'gateway':rows,'nginx':[{'request_id':ident,'status':499}],'requests':[],
        'nginx_errors':[],'services':{},'truncated':False}
    report=build_report(remote,[{'request_id':ident,'event':'bridge_request_finished'}],now)
    assert len(report['request_timelines'])==1
    assert set(report['request_timelines'][0]['layers'])=={'gateway','nginx','worker'}
    assert 'nginx_client_connection_closed' in report['request_timelines'][0]['observed_signals']


def test_collector_correlates_cancel_before_headers_and_keeps_edge_rejections():
    ident=str(uuid.uuid4());now=datetime.now(timezone.utc)
    remote={'gateway':[{'request_id':ident,'edge_id':'a'*32,'event':'request_started'}],
        'nginx':[{'request_id':'','edge_id':'a'*32,'status':499},{'request_id':'','edge_id':'b'*32,'status':413}],
        'requests':[],'nginx_errors':[],'services':{},'truncated':False}
    report=build_report(remote,[],now)
    correlated=[g for g in report['request_timelines'] if g['request_id']==ident][0]
    assert 'nginx' in correlated['layers'] and len(report['request_timelines'])==2
    edge_only=[g for g in report['request_timelines'] if g['request_id'] is None][0]
    assert edge_only['edge_id']=='b'*32 and edge_only['layers']['nginx'][0]['status']==413


@pytest.mark.parametrize('url',['http://server/v1','https://SECRET@server/v1','https://server/v1?key=SECRET'])
def test_client_probe_rejects_credentials_and_insecure_targets(url):
    with pytest.raises(ValueError):probe_tool.probe(url)


def test_client_probe_401_is_reachable_no_key_body_or_redirect(monkeypatch):
    monkeypatch.setattr(probe_tool.socket,'getaddrinfo',lambda *a,**k:[(2,1,6,'',('127.0.0.1',443))])
    class Socket:
        def __enter__(self):return self
        def __exit__(self,*a):pass
        def version(self):return 'TLSv1.3'
    class Context:
        def wrap_socket(self,*a,**k):return Socket()
    monkeypatch.setattr(probe_tool.socket,'create_connection',lambda *a,**k:Socket())
    monkeypatch.setattr(probe_tool.ssl,'create_default_context',lambda:Context())
    ident=str(uuid.uuid4());seen=[]
    class Opener:
        def open(self,request,**kwargs):
            seen.append(request)
            raise probe_tool.urllib.error.HTTPError(request.full_url,401,'secret error',{'X-Request-ID':ident},io.BytesIO(b'SECRET_BODY'))
    monkeypatch.setattr(probe_tool.urllib.request,'build_opener',lambda *a:Opener())
    report=probe_tool.probe('https://server/v1')
    assert all(r['ok'] for r in report['phases']) and report['phases'][-1]['request_id']==ident
    assert 'Authorization' not in seen[0].headers and 'SECRET' not in json.dumps(report)
    assert probe_tool.NoRedirect().redirect_request(None,None,None,None,None,None) is None
