"""Read bounded, metadata-only connection timelines from Windows and the server."""
import argparse
import collections
import gzip
import json
import shlex
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
# Explicit projection also protects reports from older logs and unexpected fields.
FIELDS = frozenset('version at event request_id instance_id process_id parent_process_id edge_id client_fp session_fp thread_fp cache_record_id '
    'method path http_status status upstream_status app_error_code error_code error_type tls_reason outcome '
    'duration duration_ms upstream_duration headers_ms upstream_headers_seconds first_byte_ms last_byte_ms '
    'bytes_received chunks_received bytes_sent upstream_bytes_received retry_after request_length '
    'protocol tls_protocol connection connection_requests request_completion response_complete '
    'disconnect_observed terminal_seen terminal_kind parse_error terminal_conflict '
    'global_active client_active global_limit client_limit active_requests max_concurrency '
    'worker_state heartbeat_fresh probe_fresh instance_consistent adapter_ready bridge_ready auth_state '
    'heartbeat_age_seconds ready access_token_changed identity_changed refresh_metadata_changed '
    'expires_at previous_expires_at last_refresh previous_last_refresh auth_file_modified_at '
    'timeout_seconds remaining_seconds consecutive_failures outage_seconds failures exit_code retry_seconds seq '
    'attempt_no max_attempts retry_wait_ms retry_budget_ms retryable request_sent stage component protocol_failure incident_id'.split())


def timestamp(value):
    try:
        d = datetime.fromisoformat(value.replace('Z', '+00:00'))
        return d.astimezone(timezone.utc) if d.tzinfo else None
    except (TypeError, ValueError, AttributeError): return None


def read_events(paths, cutoff, *, limit=6000):
    rows = []
    for path in paths:
        try:
            if path.suffix == '.gz':
                with gzip.open(path, 'rb') as f: raw = f.read(8*1024*1024)
            else:
                with path.open('rb') as f:
                    f.seek(max(0, path.stat().st_size-8*1024*1024)); raw = f.read(8*1024*1024)
            for line in raw.splitlines():
                try:
                    r = json.loads(line)
                    if not isinstance(r, dict): continue
                    at = timestamp(r.get('at'))
                    if at is None or at < cutoff: continue
                    r = {k:v for k,v in r.items() if k in FIELDS and (type(v) in (str,int,float,bool) or v is None)}
                    if 'path' in r and r['path'] not in {'/v1/models','/v1/responses','/v1/other','/other'}: r['path']='/other'
                    rows.append(r)
                except (ValueError, TypeError): continue
        except (OSError, EOFError): continue
    rows.sort(key=lambda r:r.get('at',''))
    return rows[-limit:], len(rows) > limit


REMOTE = r'''
import collections,json,re,sqlite3,subprocess,sys
from pathlib import Path
from datetime import datetime,timezone
sys.path.insert(0,sys.argv[1])
from collect_connection_diagnostics import read_events,timestamp
cutoff=timestamp(sys.argv[2]);ident=sys.argv[3]
root=Path('/var/lib/codex-relay/logs');nginx=Path('/var/log/nginx')
gateway,gt=read_events(list(root.glob('gateway-events*.jsonl')),cutoff)
edge,et=read_events(list(nginx.glob('codex-relay-transport.jsonl*')),cutoff)
worker,wt=read_events(list(Path('/var/lib/codex-relay-codex/logs').rglob('runtime-events*.jsonl')),cutoff)
# The final Windows archive overlaps the initial migrated log prefix.
worker=list({json.dumps(row,sort_keys=True):row for row in worker}.values())
worker.sort(key=lambda row:row.get('at',''))
if ident:
 gateway=[r for r in gateway if r.get('request_id')==ident]
 known_edges={r.get('edge_id') for r in gateway if r.get('edge_id')}
 edge=[r for r in edge if r.get('request_id')==ident or r.get('edge_id') in known_edges]
 worker=[r for r in worker if r.get('request_id')==ident or not r.get('request_id')]
db=sqlite3.connect('file:/var/lib/codex-relay/relay.db?mode=ro',uri=True);db.row_factory=sqlite3.Row
sql='SELECT request_id,started_at,ended_at,http_status,response_status,error_code,error_class,duration_ms,first_byte_ms,usage_status,usage_conflict FROM requests WHERE started_at >= ?'
args=[cutoff.isoformat().replace('+00:00','Z')]
if ident:sql+=' AND request_id = ?';args.append(ident)
sql+=' ORDER BY started_at DESC LIMIT 1000'
dbrows=[dict(r) for r in db.execute(sql,args)];db.close()
errors=[]
# Never export raw Nginx lines: they may contain client IPs, queries or headers.
patterns=[('tls_handshake_failed',r'SSL_do_handshake\(\) failed'),('client_body_too_large',r'client intended to send too large body'),
 ('upstream_connect_failed',r'connect\(\) failed'),('upstream_timeout',r'upstream timed out'),
 ('upstream_closed_early',r'upstream prematurely closed connection'),('client_closed_early',r'client prematurely closed connection')]
try:
 p=nginx/'error.log'
 with p.open('rb') as f:f.seek(max(0,p.stat().st_size-4*1024*1024));raw=f.read(4*1024*1024).decode(errors='replace')
 for line in raw.splitlines():
  # Interpret timestamps in the host timezone, then normalize them to UTC.
  m=re.match(r'(\d{4}/\d{2}/\d{2} \d{2}:\d{2}:\d{2})',line)
  if not m:continue
  local=datetime.strptime(m[1],'%Y/%m/%d %H:%M:%S').astimezone(timezone.utc)
  if local<cutoff:continue
  for category,pattern in patterns:
   if re.search(pattern,line):errors.append({'at':local.isoformat(),'category':category});break
except OSError:pass
services={name:subprocess.check_output(['systemctl','show',name,'-p','MainPID','-p','ActiveState','-p','ActiveEnterTimestamp'],text=True).strip() for name in ['codex-relay','codex-relay-server','codex-relay-server-v2','codex-relay-worker','codex-egress','codex-relay-dashboard','codex-relay-quota-analysis','nginx']}
print(json.dumps({'gateway':gateway,'nginx':edge,'worker':worker,'worker_host':'linux' if 'ActiveState=active' in services['codex-relay-worker'] else 'windows','requests':dbrows,'nginx_errors':errors[-1000:],'services':services,'truncated':gt or et or wt or len(dbrows)>=1000 or len(errors)>1000}))
'''


def build_report(remote, local, cutoff, ident=None, local_truncated=False):
    streams={'gateway':remote['gateway'],'nginx':remote['nginx'],'worker':local}
    groups={}
    edge_to_request={r['edge_id']:r['request_id'] for r in remote['gateway'] if r.get('edge_id') and r.get('request_id')}
    for layer, rows in streams.items():
        for r in rows:
            value=r.get('request_id') or edge_to_request.get(r.get('edge_id'))
            try:value=str(uuid.UUID(value))
            except (ValueError, TypeError, AttributeError):
                if layer!='nginx' or not r.get('edge_id') or ident:continue
                value='edge:'+r['edge_id']
            if ident and value!=ident:continue
            group=groups.setdefault(value, {'request_id':None if value.startswith('edge:') else value,
                'edge_id':r.get('edge_id'),'layers':{},'observed_signals':[]})
            group['layers'].setdefault(layer,[]).append(r)
    for row in remote['requests']:
        value=row['request_id']
        if value in groups:groups[value]['database']=row
    for g in groups.values():
        for rows in g['layers'].values():
            for r in rows:
                for field in ('error_code','app_error_code','outcome'):
                    signal=r.get(field)
                    if signal and signal not in {'ok','completed','response_complete','eof'}:
                        g['observed_signals'].append(signal)
                if r.get('status')==499:g['observed_signals'].append('nginx_client_connection_closed')
        g['observed_signals']=sorted(set(g['observed_signals']))
    return {'version':1,'created_at':datetime.now(timezone.utc).isoformat(),'since':cutoff.isoformat(),
        'request_filter':ident,'metadata_only':True,'services':remote['services'],
        'truncated':remote['truncated'] or local_truncated,'events':streams,
        'nginx_errors':remote['nginx_errors'],'database_requests':remote['requests'],
        'request_timelines':list(groups.values()),
        'summary':{'nginx_statuses':dict(collections.Counter(str(r.get('status')) for r in remote['nginx'])),
            'gateway_errors':dict(collections.Counter(r['error_code'] for r in remote['gateway'] if r.get('error_code'))),
            'worker_errors':dict(collections.Counter(r['error_code'] for r in local if r.get('error_code') and r['error_code']!='ok'))},
        'limits':['Only retained events are included; missing layers do not prove a request was never sent.',
            'Cancellation and Nginx 499 do not identify why the client connection closed.',
            'DNS/TCP/TLS failures before HTTP require a probe from the affected client device.',
            'Client disconnect observed by the gateway means its direct peer (Nginx); bridge peer is the gateway.']}


def main():
    import subprocess
    p=argparse.ArgumentParser(description=__doc__);p.add_argument('--minutes',type=int,default=30)
    p.add_argument('--ssh-target',required=True,help='Administrator SSH target, e.g. admin@relay.example.com')
    p.add_argument('--request-id');p.add_argument('--output',type=Path,default=ROOT/'reports/connection-diagnostics.json')
    a=p.parse_args()
    if not __import__('re').fullmatch(r'[a-zA-Z0-9_][a-zA-Z0-9_-]*@[a-zA-Z0-9][a-zA-Z0-9.:-]*',a.ssh_target):
        p.error('invalid_ssh_target')
    def ssh(command,data=None):
        result=subprocess.run(['ssh','-o','BatchMode=yes','-o','StrictHostKeyChecking=yes',
            '-o','ConnectTimeout=5',a.ssh_target,command],input=data,capture_output=True,timeout=60)
        if result.returncode:raise RuntimeError('remote_diagnostic_collection_failed')
        return result.stdout
    if not 1<=a.minutes<=1440:p.error('minutes_must_be_between_1_and_1440')
    ident=str(uuid.UUID(a.request_id)) if a.request_id else None
    cutoff=datetime.now(timezone.utc)-timedelta(minutes=a.minutes)
    # A copy of this self-contained reader is staged with no configuration or secrets.
    stage='/tmp/relay-log-reader-'+uuid.uuid4().hex[:12]
    ssh('mkdir -m 700 '+shlex.quote(stage))
    ssh('cat > '+shlex.quote(stage+'/collect_connection_diagnostics.py'),Path(__file__).read_bytes())
    remote=json.loads(ssh('sudo -n /opt/codex-relay/.venv/bin/python -c '+shlex.quote(REMOTE)+' '+shlex.quote(stage)+' '+shlex.quote(cutoff.isoformat())+' '+shlex.quote(ident or '')))
    from windows.config import load_config
    from windows.local_control import default_config
    if remote.get('worker_host')=='linux':
        local,truncated=remote['worker'],False
    else:
        config=load_config(default_config())
        local,truncated=read_events(list((config.runtime_dir.parent/'logs').glob('runtime-events*.jsonl')),cutoff)
    if ident:local=[r for r in local if r.get('request_id')==ident or not r.get('request_id')]
    result=build_report(remote,local,cutoff,ident,truncated)
    result['worker_host']=remote.get('worker_host','windows')
    a.output.parent.mkdir(parents=True,exist_ok=True);a.output.write_text(json.dumps(result,indent=2),encoding='utf-8')
    print(json.dumps({'report':str(a.output),'request_timelines':len(result['request_timelines']),
        'summary':result['summary'],'truncated':result['truncated']}))


if __name__=='__main__':main()
