"""Activation must drain current work, release admission and preserve other services."""
import json
import sys
from types import SimpleNamespace

import pytest

pytestmark=pytest.mark.skipif(sys.platform!='linux',reason='Linux activation service')
if sys.platform=='linux':
    from linux_worker import activate_timeout as activation


@pytest.mark.parametrize('restart_failure',[False,True])
def test_waits_for_current_work_and_always_releases_gate(tmp_path,monkeypatch,restart_failure):
    monkeypatch.setattr(activation,'ROOT',tmp_path)
    monkeypatch.setattr(activation,'REPORT',tmp_path/'report.json')
    monkeypatch.setattr(activation,'PAUSE',tmp_path/'pause')
    monkeypatch.setattr(activation.LinuxConfig,'model_validate_json',lambda _:SimpleNamespace(adapter=SimpleNamespace(safe_retry_budget_seconds=60)))
    original=activation.Path.read_text
    def read(path,*args,**kwargs):
        if str(path)=='/var/lib/codex-relay-codex/worker.json':return '{}'
        if str(path)=='/etc/codex-relay/server-linux.config.json':return json.dumps({'worker_auth_secret_file':str(tmp_path/'key'),'db_path':str(tmp_path/'db'),'worker_id':'worker'})
        return original(path,*args,**kwargs)
    monkeypatch.setattr(activation.Path,'read_text',read)
    (tmp_path/'key').write_text('synthetic-key')
    active=[2,2,1,0,0,0];restarted=[]
    def get_health(*args,**kwargs):
        n=active.pop(0) if active else 0
        instance='new' if restarted else 'old'
        return Response({'instance_id':instance,'local':{'active_requests':n,'bridge_ready':True,'adapter_ready':True}})
    class Response:
        def __init__(self,value):self.value=json.dumps(value)
        def read(self):return self.value
        def __enter__(self):return self
        def __exit__(self,*args):pass
    monkeypatch.setattr(activation.urllib.request,'build_opener',lambda *a:SimpleNamespace(open=get_health))
    monkeypatch.setattr(activation.time,'sleep',lambda _:None)
    def command(args,**kwargs):
        assert args==['systemctl','restart','codex-relay-worker']
        assert not active and activation.PAUSE.exists()
        if restart_failure:raise RuntimeError('synthetic failure')
        restarted.append(True)
    monkeypatch.setattr(activation.subprocess,'run',command)
    monkeypatch.setattr(activation.subprocess,'check_output',lambda args,**kwargs:str(2 if args[2]=='codex-relay-worker' and restarted else 1))
    db=SimpleNamespace(execute=lambda *a:SimpleNamespace(fetchone=lambda:('new','new',1)),close=lambda:None)
    monkeypatch.setattr(activation.sqlite3,'connect',lambda *a,**kwargs:db)
    if restart_failure:
        with pytest.raises(RuntimeError):activation.main()
    else:activation.main()
    report=json.loads((tmp_path/'report.json').read_text())
    assert report['state']==('failed' if restart_failure else 'activated')
    assert not activation.PAUSE.exists()
    if not restart_failure:
        assert report['old_active_requests_drained'] and report['other_services_unchanged']
