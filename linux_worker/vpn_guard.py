"""Persistent VPN meter, with optional kernel-enforced traffic budget."""
import argparse
import fcntl
import json
import math
import os
import re
import subprocess
import time
import urllib.request
from pathlib import Path

from vpn_monitor.meter import Meter, atomic_json, now

ROOT = Path('/var/lib/codex-vpn-guard')
MARK = 0x40000000
RESERVE = 1024*1024


def kernel_status():
    raw = json.loads(subprocess.check_output(['nft','-j','list','table','inet','codex_vpn_guard'],text=True,stderr=subprocess.DEVNULL))
    quota = next((x['quota'] for x in raw['nftables'] if x.get('quota',{}).get('name')=='vpn_budget'),None)
    counters = {x['counter']['name']:x['counter']['bytes'] for x in raw['nftables'] if 'counter' in x}
    used = quota['used'] if quota else counters['vpn_upload']+counters['vpn_download']+counters.get('vpn_total_offset',0)
    return {'used':used,'limit':quota['bytes'] if quota else None,**counters}


def rules(uid, limit, used, counters=None):
    counters = counters or {}
    def counter(name):
        value = counters.get(name,0)
        if isinstance(value,dict):
            return f' counter {name} {{ packets {value.get("packets",0)} bytes {value["bytes"]}; }}\n'
        return f' counter {name} {{ packets 0 bytes {value}; }}\n'
    quota = f' quota vpn_budget {{ over {limit} bytes used {used} bytes; }}\n' if limit is not None else ''
    declarations = ''.join(counter(n) for n in ('vpn_upload','vpn_download','vpn_blocked'))
    if limit is None:
        total = sum(v.get('bytes',0) if isinstance(v,dict) else v for n,v in counters.items() if n in ('vpn_upload','vpn_download'))
        declarations += f' counter vpn_total_offset {{ packets 0 bytes {max(0,used-total)}; }}\n'
    output_cutoff = f'  oifname != "lo" meta skuid {uid} quota name "vpn_budget" counter name "vpn_blocked" drop\n' if limit is not None else ''
    input_cutoff = '  iifname != "lo" ct mark & 0x40000000 != 0 quota name "vpn_budget" counter name "vpn_blocked" drop\n' if limit is not None else ''
    return f'''table inet codex_vpn_guard {{
{quota}{declarations}\
 chain output {{
  type filter hook output priority 110; policy accept;
  oifname != "lo" meta skuid {uid} ct mark set ct mark | 0x40000000
{output_cutoff}  oifname != "lo" meta skuid {uid} counter name "vpn_upload"
 }}
 chain input {{
  type filter hook input priority 110; policy accept;
{input_cutoff}  iifname != "lo" ct mark & 0x40000000 != 0 counter name "vpn_download"
 }}
}}
'''


def ensure():
    ROOT.mkdir(mode=0o700,exist_ok=True)
    with (ROOT/'install.lock').open('a') as lock:
        fcntl.flock(lock,fcntl.LOCK_EX)
        budget = json.loads((ROOT/'budget.json').read_text())
        try:
            existing = kernel_status()
            if existing['limit'] != budget['limit_bytes']:
                raise RuntimeError('vpn_budget_kernel_mismatch')
            return budget, existing
        except subprocess.CalledProcessError:
            used = max(budget.get('reserved_bytes',0),budget['limit_bytes'] if budget.get('tripped') and budget['limit_bytes'] is not None else 0)
            text = rules(budget['core_uid'],budget['limit_bytes'],used,budget.get('counter_checkpoint'))
            subprocess.run(['nft','-c','-f','-'],input=text.encode(),capture_output=True,check=True)
            subprocess.run(['nft','-f','-'],input=text.encode(),capture_output=True,check=True)
            return budget, kernel_status()


def checkpoint(budget, used, status=None):
    # Reserve ahead so a sudden reboot cannot reset the budget. Overcounts by
    # between one and two MiB at a cold restart; same kernel object is exact.
    reserved = math.ceil((used+RESERVE)/RESERVE)*RESERVE
    if budget['limit_bytes'] is not None:reserved = min(budget['limit_bytes'],reserved)
    if reserved > budget.get('reserved_bytes',0):
        budget['reserved_bytes'] = reserved
        if status:budget['counter_checkpoint'] = {n:status[n] for n in ('vpn_upload','vpn_download','vpn_blocked')}
        atomic_json(ROOT/'budget.json',budget)


def latch(budget, reason):
    budget.update(tripped=True,stopped_at=now(),stop_reason=reason)
    atomic_json(ROOT/'budget.json',budget)
    (ROOT/'tripped').touch(mode=0o600)
    subprocess.run(['systemctl','stop','codex-egress'],check=True,timeout=20)


def socket_owners():
    raw = subprocess.check_output(['ss','-ntp','( dport = :17890 )'],text=True,timeout=2)
    result = {}
    for line in raw.splitlines():
        port = re.search(r'127\.0\.0\.1:(\d+)\s+127\.0\.0\.1:17890',line)
        pid = re.search(r'pid=(\d+)',line)
        if not port or not pid:
            continue
        path = Path('/proc')/pid[1]
        try:
            exe = (path/'exe').resolve().name
            command = (path/'cmdline').read_bytes().split(b'\0')
            if exe.startswith('python') and b'linux_worker.worker' in command:
                label = 'relay_inference_worker (Python)'
            elif exe=='codex' and b'app-server' in command:
                label = 'official_codex_auth_and_quota'
            else:
                label = exe
            result[port[1]] = label
        except OSError:
            pass
    return result


def watch():
    budget,_ = ensure()
    meter_path = ROOT/'meter.json'
    if meter_path.exists():
        meter = Meter(json.loads(meter_path.read_text()))
    else:
        baseline = json.loads((ROOT/'meter-baseline.json').read_text()) if (ROOT/'meter-baseline.json').exists() else None
        meter = Meter(baseline=baseline)
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    while True:
        try:
            status = kernel_status()
            checkpoint(budget,status['used'],status)
            if budget['limit_bytes'] is not None and (budget.get('tripped') or status['used'] >= budget['limit_bytes']):
                if not budget.get('tripped'):
                    latch(budget,'raw_vpn_budget_reached')
                state = meter.summary()|{'kernel':status,'limit_bytes':budget['limit_bytes'],'paused':True,'stop_reason':budget.get('stop_reason')}
                atomic_json(ROOT/'status.json',state)
                time.sleep(2)
                continue
            cfg = json.loads(Path('/etc/codex-egress/config.json').read_text())
            req = urllib.request.Request('http://127.0.0.1:17990/connections',headers={'Authorization':'Bearer '+cfg['secret']})
            with opener.open(req,timeout=2) as response:
                snapshot = json.load(response)
            owners = socket_owners()
            meter.observe(snapshot,lambda r:owners.get(str((r.get('metadata') or {}).get('sourcePort'))))
            atomic_json(meter_path,meter.data)
            state = meter.summary()|{'kernel':status,'limit_bytes':budget['limit_bytes'],'paused':False,
                'wire_total_bytes':status['vpn_upload']+status['vpn_download'],
                'remaining_budget_bytes':max(0,budget['limit_bytes']-status['used']) if budget['limit_bytes'] is not None else None,
                'automatic_cutoff':budget['limit_bytes'] is not None,
                'threshold_basis':'Kernel upload + download, including encrypted transport overhead; both server nodes are counted.'}
            atomic_json(ROOT/'status.json',state)
        except (OSError,ValueError,KeyError,StopIteration,subprocess.SubprocessError):
            # Finite budgets fail closed. Monitor-only errors must not stop VPN.
            try:
                ensure()
            except Exception:
                if budget['limit_bytes'] is not None:latch(budget,'kernel_guard_unavailable')
            atomic_json(ROOT/'last-monitor-error.json',{'at':now(),'event':'monitor_read_failed','kernel_limit_retained':budget['limit_bytes'] is not None})
        time.sleep(1)


def main():
    parser=argparse.ArgumentParser();parser.add_argument('mode',choices=['ensure','watch']);args=parser.parse_args()
    if args.mode=='ensure':
        budget,status=ensure()
        if budget['limit_bytes'] is not None and (budget.get('tripped') or status['used']>=budget['limit_bytes']):
            budget.update(tripped=True,stop_reason=budget.get('stop_reason','raw_vpn_budget_reached'))
            atomic_json(ROOT/'budget.json',budget)
            (ROOT/'tripped').touch(mode=0o600)
            raise SystemExit(1)
    else:
        watch()


if __name__=='__main__':
    main()
