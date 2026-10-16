"""One-shot, server-owned timeout activation after the Worker becomes idle."""
import fcntl
import json
import os
import sqlite3
import subprocess
import time
import urllib.request
from pathlib import Path

from linux_worker.config import LinuxConfig
from vpn_monitor.meter import atomic_json, now

ROOT=Path('/var/lib/codex-relay')
REPORT=ROOT/'timeout-60-activation.json'
PAUSE=ROOT/'admission.pause'


def main():
    os.umask(0o077)
    with (ROOT/'timeout-activation.lock').open('a') as lock:
        fcntl.flock(lock,fcntl.LOCK_EX|fcntl.LOCK_NB)
        config=LinuxConfig.model_validate_json(Path('/var/lib/codex-relay-codex/worker.json').read_text())
        assert config.adapter.safe_retry_budget_seconds==60
        server=json.loads(Path('/etc/codex-relay/server-linux.config.json').read_text())
        key=Path(server['worker_auth_secret_file']).read_text().strip()
        opener=urllib.request.build_opener(urllib.request.ProxyHandler({}))
        def health():
            req=urllib.request.Request('http://127.0.0.1:8765/internal/v1/health',headers={'Authorization':'Bearer '+key})
            with opener.open(req,timeout=2) as response:return json.load(response)
        def pids():
            return {unit:int(subprocess.check_output(['systemctl','show',unit,'-p','MainPID','--value'],text=True).strip())
                    for unit in ['codex-relay-worker','codex-relay-server-v2','codex-egress','nginx']}
        old=health()['instance_id'];before=pids()
        report={'started_at':now(),'state':'waiting_for_idle','timeout_seconds':60,
                'old_instance_id':old,'pids_before':before}
        atomic_json(REPORT,report)
        owns_pause=False
        try:
            while True:
                # Existing requests retain the deadline captured when they started.
                try:active=health()['local']['active_requests']
                except OSError:
                    time.sleep(1);continue
                if active or PAUSE.exists():
                    time.sleep(.2);continue
                # Briefly queue new requests only after observing an idle Worker.
                fd=os.open(PAUSE,os.O_CREAT|os.O_EXCL|os.O_WRONLY,0o644)
                os.close(fd);PAUSE.chmod(0o644);owns_pause=True
                started=time.monotonic();drain_until=started+25
                while health()['local']['active_requests'] and time.monotonic()<drain_until:time.sleep(.2)
                if health()['local']['active_requests']:
                    PAUSE.unlink(missing_ok=True);owns_pause=False
                    continue
                break
            report['state']='activating';atomic_json(REPORT,report)
            subprocess.run(['systemctl','restart','codex-relay-worker'],capture_output=True,check=True,timeout=25)
            deadline=time.monotonic()+30
            db=sqlite3.connect('file:'+server['db_path']+'?mode=ro',uri=True)
            try:
                while True:
                    try:
                        h=health()
                        row=db.execute('select current_instance_id,probe_instance_id,probe_ok from workers where worker_id=?',(server['worker_id'],)).fetchone()
                        if h['instance_id']!=old and h['local']['bridge_ready'] and h['local']['adapter_ready'] and row and row[0]==row[1]==h['instance_id'] and row[2]:break
                    except OSError:pass
                    if time.monotonic()>deadline:raise RuntimeError('activation_readiness_timeout')
                    time.sleep(.2)
            finally:db.close()
            after=pids()
            report.update(state='activated',finished_at=now(),new_instance_id=h['instance_id'],
                          old_active_requests_drained=True,queue_seconds=round(time.monotonic()-started,3),
                          pids_after=after,other_services_unchanged=all(before[u]==after[u] for u in before if u!='codex-relay-worker'))
            atomic_json(REPORT,report)
        except Exception as exc:
            report.update(state='failed',finished_at=now(),error_type=type(exc).__name__)
            atomic_json(REPORT,report)
            raise
        finally:
            if owns_pause:PAUSE.unlink(missing_ok=True)


if __name__=='__main__':main()
