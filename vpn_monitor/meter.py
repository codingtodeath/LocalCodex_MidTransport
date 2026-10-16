import json
import os
import time
from datetime import datetime, timezone
from pathlib import Path


def now():
    return datetime.now(timezone.utc).isoformat(timespec='seconds')


def atomic_json(path, data):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix('.tmp')
    tmp.write_text(json.dumps(data, ensure_ascii=True), encoding='utf-8')
    if os.name != 'nt':
        tmp.chmod(0o600)
    replace_file(tmp, path)


def replace_file(source, target):
    # Windows readers / antivirus may briefly deny replacing an open file.
    for attempt in range(10):
        try:
            os.replace(source,target)
            return
        except PermissionError:
            if os.name!='nt' or attempt==9:raise
            time.sleep(.05)


class Meter:
    """Global counters include missed short connections; never silently drop them."""
    def __init__(self, data=None, baseline=None):
        self.data = data or {'version':1, 'started_at':now(), 'last':baseline,
            'upload_bytes':0, 'download_bytes':0, 'apps':{}, 'seen':{}, 'counter_resets':0}

    def observe(self, snapshot, resolver=None):
        current = {k:int(snapshot.get(k) or 0) for k in ['uploadTotal','downloadTotal']}
        old = self.data.get('last')
        if old is None:
            self.data['last'] = current
            # Pre-monitor active connections are baselined, not charged twice.
            for row in snapshot.get('connections') or []:
                self.data['seen'][row['id']] = [int(row.get('upload') or 0), int(row.get('download') or 0)]
            return
        reset = any(current[k] < int(old.get(k) or 0) for k in current)
        if reset:
            self.data['counter_resets'] += 1
            self.data['seen'] = {}
        self.data['upload_bytes'] += current['uploadTotal'] if reset else current['uploadTotal']-int(old.get('uploadTotal') or 0)
        self.data['download_bytes'] += current['downloadTotal'] if reset else current['downloadTotal']-int(old.get('downloadTotal') or 0)
        self.data['last'] = current
        active_ids = set()
        for row in snapshot.get('connections') or []:
            ident = row['id'];active_ids.add(ident)
            values = [int(row.get('upload') or 0), int(row.get('download') or 0)]
            previous = self.data['seen'].get(ident, [0,0])
            deltas = [max(0,v-p) for v,p in zip(values,previous)]
            self.data['seen'][ident] = values
            chains = row.get('chains') or []
            if not chains or chains[0] in {'DIRECT','REJECT','REJECT-DROP','PASS'}:
                continue
            metadata = row.get('metadata') or {}
            app = (resolver(row) if resolver else None) or metadata.get('process') or Path(metadata.get('processPath') or '').name or 'unknown_process'
            app = str(app)[:160]
            key = app
            item = self.data['apps'].setdefault(key, {'application':app,'upload_bytes':0,'download_bytes':0,'routes':{}})
            item['upload_bytes'] += deltas[0];item['download_bytes'] += deltas[1]
            route = str(chains[0])[:160]
            item['routes'][route] = item['routes'].get(route,0)+sum(deltas)
        self.data['seen'] = {k:v for k,v in self.data['seen'].items() if k in active_ids}

    def summary(self):
        total = self.data['upload_bytes']+self.data['download_bytes']
        apps = sorted(self.data['apps'].values(),key=lambda x:x['upload_bytes']+x['download_bytes'],reverse=True)
        attributed = sum(x['upload_bytes']+x['download_bytes'] for x in apps)
        return {'started_at':self.data['started_at'],'observed_at':now(),
            'payload_upload_bytes':self.data['upload_bytes'],'payload_download_bytes':self.data['download_bytes'],
            'payload_total_bytes':total,'proxy_application_rankings':apps,
            'unattributed_or_direct_payload_bytes':max(0,total-attributed),
            'counter_resets':self.data['counter_resets'],
            'attribution_note':'Proxy application rankings are sampled; short connections can remain unattributed. Global counters retain all observed traffic, including DIRECT on a mixed local configuration.'}
