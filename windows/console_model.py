"""Local control-panel data. Never refresh login or include secrets in snapshots."""
import json
import os
import subprocess
import sys
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from urllib.parse import urlsplit

import httpx

from .config import load_config, read_key
from .local_control import default_config, is_running
from .session_info import expiry_info
from .setup import ROOT

LOCAL_TZ = timezone(timedelta(hours=8))


def parsed_time(value):
    try:
        d=datetime.fromisoformat(value.replace('Z','+00:00'))
        return d if d.tzinfo else None
    except (TypeError, ValueError, AttributeError):return None


def display_time(value):
    d=parsed_time(value)
    return d.astimezone(LOCAL_TZ).strftime('%m-%d %H:%M:%S') if d else '—'


def recent_events(path, limit=60):
    try:
        with path.open('rb') as f:
            f.seek(max(0,path.stat().st_size-128*1024));raw=f.read(128*1024)
        rows=[]
        allowed={'at','event','http_status','error_code','error_type','duration_ms','consecutive_failures','failures','ready','exit_code','terminal_seen',
                 'attempt_no','max_attempts','retry_wait_ms','retry_budget_ms','retryable','request_sent','stage','component'}
        for line in raw.splitlines():
            try:
                row=json.loads(line)
                if isinstance(row,dict):rows.append({k:v for k,v in row.items() if k in allowed})
            except (ValueError,TypeError):pass
        return rows[-limit:]
    except OSError:return []


def worker_started(config, status):
    if parsed_time(status.get('started_at')):return parsed_time(status['started_at'])
    # Older running Workers did not have started_at in their status file.
    path=config.runtime_dir.parent/'logs/runtime-events.jsonl'
    try:
        with path.open('rb') as f:
            f.seek(max(0,path.stat().st_size-1024*1024));raw=f.read(1024*1024)
        for line in reversed(raw.splitlines()):
            try:r=json.loads(line)
            except (ValueError,TypeError):continue
            if not isinstance(r,dict):continue
            if r.get('event')=='worker_started' and r.get('instance_id')==status.get('instance_id'):
                return parsed_time(r.get('at'))
    except OSError:pass
    return None


class ConsoleBackend:
    def __init__(self, config_path=None):
        self.config_path=Path(config_path or default_config())
        self.remote={'ok':False,'checked_at':None,'connected':False,'state':'unknown'}
        self.remote_at=-float('inf')

    def config(self):return load_config(self.config_path)

    def server_status(self, origin, ca_file=None):
        try:
            import ssl
            verify=ssl.create_default_context(cafile=str(ca_file)) if ca_file else True
            with httpx.Client(trust_env=False,follow_redirects=False,timeout=5,verify=verify) as client:
                with client.stream('GET',origin+'/public/api/overview') as r:
                    if r.status_code!=200:raise ValueError('server_unavailable')
                    raw=bytearray()
                    for chunk in r.iter_bytes():
                        if len(raw)+len(chunk)>1024*1024:raise ValueError('response_too_large')
                        raw.extend(chunk)
            obj=json.loads(raw);relay=obj.get('relay',{})
            self.remote={'ok':True,'checked_at':datetime.now(timezone.utc).isoformat(),
                'connected':relay.get('connected') is True,
                'state':relay.get('state') if relay.get('state') in {'online','offline','degraded','unknown'} else 'unknown'}
        except Exception:
            self.remote={'ok':False,'checked_at':datetime.now(timezone.utc).isoformat(),'connected':False,'state':'unknown'}
        self.remote_at=time.monotonic()

    def snapshot(self, force_remote=False):
        c=self.config();now=datetime.now(timezone.utc)
        try:s=json.loads((c.runtime_dir/'status.json').read_text(encoding='utf-8'))
        except (OSError,ValueError):s={}
        if not isinstance(s,dict):s={}
        running=is_running(c)
        observed=parsed_time(s.get('observed_at'))
        fresh=bool(running and observed and -5 <= (now-observed).total_seconds() <= 15)
        local=s.get('local',{}) if isinstance(s.get('local'),dict) else {}
        u=urlsplit(c.telemetry.url or '')
        origin=(u.scheme+'://'+u.netloc) if u.scheme=='https' and u.hostname and not u.username and not u.password else ''
        if origin and (force_remote or time.monotonic()-self.remote_at>=15):
            self.server_status(origin,c.telemetry.ca_cert_file)
        credential=expiry_info(c)
        started=worker_started(c,s) if running else None
        pending=bool(started and (ROOT/'windows/quota_client.py').stat().st_mtime>started.timestamp())
        return {'migrated_to_server':(c.runtime_dir/'server-migration.json').exists(),
            'running':running,'fresh':fresh,'openai_ready':fresh and local.get('adapter_ready') is True,
            'auth_state':local.get('auth_state') if fresh else 'unknown',
            'bridge_ready':fresh and local.get('bridge_ready') is True,
            'tunnel_ready':fresh and s.get('tunnel_process_running') is True,
            'telemetry_ok':fresh and s.get('telemetry_ok') is True,
            'active_requests':local.get('active_requests',0) if fresh else None,
            'max_concurrency':c.bridge.max_concurrency,'last_success':local.get('last_inference_success_at'),
            'observed_at':s.get('observed_at'),'instance_id':s.get('instance_id'),
            'started_at':started.isoformat() if started else None,
            'uptime_seconds':max(0,int((now-started).total_seconds())) if started else None,
            'remote':dict(self.remote),'origin':origin,'base_url':origin+'/v1' if origin else '未配置',
            'key_available':(c.runtime_dir.parent/'secrets/client-gw.key').is_file(),
            'credential':credential,'proxy':c.adapter.outbound_proxy or '直连',
            'diagnostic_ok':s.get('diagnostic_log',{}).get('write_error') is False if fresh else None,
            'quota_status':s.get('quota_status') if fresh else None,'update_pending':pending,
            'events':recent_events(c.runtime_dir.parent/'logs/runtime-events.jsonl')}

    def key(self):
        c=self.config()
        return read_key(c.runtime_dir.parent/'secrets/client-gw.key','gw_')

    def log_directory(self):return self.config().runtime_dir.parent/'logs'

    def control(self, operation):
        if operation not in {'start','stop','restart'}:raise ValueError('invalid_control_operation')
        # Use the console interpreter for captured status, but never create a window.
        python=str(Path(sys.executable).with_name('python.exe')) if os.name=='nt' else sys.executable
        for command in (['stop','start'] if operation=='restart' else [operation]):
            r=subprocess.run([python,'-m','windows.local_control',command,'--config',str(self.config_path)],
                cwd=ROOT,stdin=subprocess.DEVNULL,capture_output=True,timeout=40,
                creationflags=subprocess.CREATE_NO_WINDOW if os.name=='nt' else 0)
            if r.returncode:
                raise RuntimeError('控制未完成，请刷新状态并检查运行日志。')
