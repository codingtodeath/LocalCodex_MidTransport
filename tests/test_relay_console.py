import json
import subprocess
import tkinter as tk
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest
from windows import console_model
from windows.console_model import ConsoleBackend
from windows.relay_console import RelayConsole


def snapshot(**updates):
    r={'running':True,'fresh':True,'openai_ready':True,'auth_state':'ready','tunnel_ready':True,
       'telemetry_ok':True,'active_requests':2,'max_concurrency':3,'last_success':None,'observed_at':None,
       'remote':{'ok':True,'connected':True,'checked_at':None},'base_url':'https://server/v1','origin':'https://server',
       'key_available':True,'credential':{'status':'available','expires_at_local':'2026-10-11 16:50:58 +0800','remaining_minutes':120},
       'proxy':'http://127.0.0.1:7890','diagnostic_ok':True,'quota_status':'ok','uptime_seconds':60,'events':[],'update_pending':False}
    r.update(updates);return r


def test_status_does_not_serialize_keys_and_marks_stale_status_offline(config,monkeypatch):
    c=config;c.telemetry.url=None
    key='gw_'+'X'*43;(c.runtime_dir.parent/'secrets/client-gw.key').write_text(key)
    old=datetime.now(timezone.utc)-timedelta(minutes=3)
    (c.runtime_dir/'status.json').write_text(json.dumps({'observed_at':old.isoformat(),'local':{'adapter_ready':True},'tunnel_process_running':True}))
    backend=ConsoleBackend();monkeypatch.setattr(backend,'config',lambda:c)
    monkeypatch.setattr(console_model,'is_running',lambda _:True)
    result=backend.snapshot()
    assert result['running'] and not result['fresh'] and not result['openai_ready'] and not result['tunnel_ready']
    assert key not in json.dumps(result) and 'access_token' not in json.dumps(result)


def test_control_hides_windows_and_propagates_failure(monkeypatch):
    backend=ConsoleBackend();seen=[]
    def run(command,**kwargs):
        seen.append((command,kwargs));return subprocess.CompletedProcess(command,0)
    monkeypatch.setattr(console_model.subprocess,'run',run)
    backend.control('restart')
    assert [c[0][3] for c in seen]==['stop','start']
    assert all(k['creationflags']==subprocess.CREATE_NO_WINDOW for _,k in seen)
    monkeypatch.setattr(console_model.subprocess,'run',lambda *a,**k:subprocess.CompletedProcess(a[0],1))
    with pytest.raises(RuntimeError):backend.control('start')


def test_logs_are_bounded_and_drop_input_or_auth_fields(tmp_path):
    path=tmp_path/'events.jsonl'
    path.write_text('broken json\n'+json.dumps({'event':'inference_opened','at':'2026-10-06T00:00:00Z','input':'SECRET','accessToken':'SECRET','http_status':200})+'\n')
    rows=console_model.recent_events(path)
    assert len(rows)==1 and 'SECRET' not in json.dumps(rows)


@pytest.fixture
def panel():
    class Backend:
        commands=[]
        def key(self):return 'gw_SYNTHETIC_SECRET'
        def control(self,operation):self.commands.append(operation)
    try:root=tk.Tk()
    except tk.TclError:pytest.skip('Tk display unavailable')
    root.withdraw();ui=RelayConsole(root,Backend(),polling=False)
    yield ui
    if not ui.closed.is_set():ui.close()


def test_panel_status_buttons_masking_and_close_preserves_worker(panel):
    panel.render(snapshot())
    assert panel.start_button.instate(['disabled']) and not panel.stop_button.instate(['disabled'])
    assert 'SYNTHETIC_SECRET' not in panel.vars['key'].get()
    panel.toggle_key();assert panel.vars['key'].get()=='gw_SYNTHETIC_SECRET'
    panel.toggle_key();assert 'SYNTHETIC_SECRET' not in panel.vars['key'].get()
    panel.render(snapshot(running=False,fresh=False,openai_ready=False,tunnel_ready=False))
    assert panel.stop_button.instate(['disabled']) and not panel.start_button.instate(['disabled'])
    panel.close();assert panel.backend.commands==[]


def test_panel_does_not_claim_online_when_openai_unreachable(panel):
    panel.render(snapshot(openai_ready=False))
    assert panel.pill.cget('text')!='中继在线' and panel.cards['openai'].cget('text')=='未就绪'


async def test_quota_subprocess_is_hidden(config,monkeypatch):
    import asyncio
    from windows.quota_client import QuotaClient
    captured={}
    class Process:
        pid=1;returncode=0
        stdout=asyncio.StreamReader()
    Process.stdout.feed_eof()
    async def create(*args,**kwargs):captured.update(kwargs);return Process()
    async def request(*args,**kwargs):return {}
    async def send(*args,**kwargs):return None
    monkeypatch.setattr(asyncio,'create_subprocess_exec',create)
    c=QuotaClient(['mock'],config.quota.codex_home)
    monkeypatch.setattr(c,'request',request);monkeypatch.setattr(c,'_send',send)
    await c.start();await c.close()
    assert captured['creationflags']==subprocess.CREATE_NO_WINDOW
