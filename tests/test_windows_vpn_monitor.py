import json
import io
import sys
from types import SimpleNamespace

import pytest

pytestmark=pytest.mark.skipif(sys.platform!='win32',reason='Windows background monitor')
if sys.platform=='win32':
    from windows import vpn_monitor


def test_half_written_configuration_does_not_exit(tmp_path,monkeypatch):
    config=tmp_path/'config.yaml';config.write_text('secret: [unfinished',encoding='utf-8')
    monkeypatch.setattr(vpn_monitor.subprocess,'run',lambda *a,**k:SimpleNamespace(returncode=1))
    vpn_monitor.run(tmp_path,config,once=True)
    status=json.loads((tmp_path/'local-status.json').read_text())
    assert status['monitor_error'] is True
    assert status['automatic_cutoff'] is False
    events=(tmp_path/'monitor-errors.jsonl').read_text()
    assert 'local_snapshot_failed' in events
    assert 'unfinished' not in events


def test_unexpected_exception_is_logged_and_retried(tmp_path,monkeypatch):
    monkeypatch.setenv('LOCALAPPDATA',str(tmp_path))
    monkeypatch.setattr(sys,'argv',['vpn_monitor'])
    calls=[]
    def transient(*a,**k):
        calls.append(1)
        if len(calls)==1:raise RuntimeError('private-message-must-not-be-recorded')
    monkeypatch.setattr(vpn_monitor,'run',transient)
    monkeypatch.setattr(vpn_monitor.time,'sleep',lambda _:None)
    vpn_monitor.main()
    assert len(calls)==2
    events=(tmp_path/'CodexRelay/vpn-monitor/monitor-errors.jsonl').read_text()
    assert 'monitor_loop_recovered' in events
    assert 'private-message' not in events


def test_null_connection_counters_are_zero(tmp_path,monkeypatch):
    config=tmp_path/'config.yaml';config.write_text('external-controller: 127.0.0.1:9090\n',encoding='utf-8')
    snapshot={'uploadTotal':0,'downloadTotal':0,'connections':[{'id':'test','upload':None,'download':None,'metadata':{'process':'test.exe'},'chains':['TestNode']}]}
    opener=SimpleNamespace(open=lambda *a,**k:io.StringIO(json.dumps(snapshot)))
    monkeypatch.setattr(vpn_monitor.urllib.request,'build_opener',lambda *a:opener)
    monkeypatch.setattr(vpn_monitor.subprocess,'run',lambda *a,**k:SimpleNamespace(returncode=1))
    vpn_monitor.run(tmp_path,config,once=True)
    assert json.loads((tmp_path/'local-status.json').read_text())['monitor_error'] is False
    flow=json.loads((tmp_path/'active-flows.json').read_text())['connections'][0]
    assert flow['upload_bytes']==flow['download_bytes']==0


def test_server_unlimited_monitor_display(tmp_path):
    target=tmp_path/'report.html'
    vpn_monitor.render(target,{'observed_at':'2026-10-13T02:00:00Z'},
                       {'limit_bytes':None,'kernel':{'used':2100000000},'paused':False})
    text=target.read_text(encoding='utf-8')
    assert '服务器：只监控，不自动停用' in text
    assert '2,100.00 MB · 无流量上限' in text
