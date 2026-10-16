"""Monitor only: never applies the server's traffic cap to Windows."""
import argparse
import html
import json
import msvcrt
import os
import re
import subprocess
import time
import traceback
import urllib.request
from pathlib import Path

import yaml

from vpn_monitor.meter import Meter, atomic_json, now, replace_file


def record_error(root,event,error):
    # Stack locations only: parser messages can contain private YAML values.
    data={'at':now(),'event':event,'error_type':type(error).__name__,
          'frames':[{'file':Path(f.filename).name,'line':f.lineno,'function':f.name} for f in traceback.extract_tb(error.__traceback__)[-8:]]}
    try:
        path=root/'monitor-errors.jsonl'
        if path.exists() and path.stat().st_size>1000000:replace_file(path,path.with_suffix('.previous.jsonl'))
        with path.open('a',encoding='utf-8') as stream:stream.write(json.dumps(data,ensure_ascii=True)+'\n')
    except OSError:pass


def render(target, local, server, flows=None):
    def mb(n):return f'{int(n or 0)/1000000:,.2f} MB'
    def table(rows):
        return '<table><tr><th>应用</th><th>上传</th><th>下载</th><th>合计</th></tr>'+''.join(
            '<tr><td>'+html.escape(r['application'])+'</td><td>'+mb(r['upload_bytes'])+'</td><td>'+mb(r['download_bytes'])+'</td><td>'+mb(r['upload_bytes']+r['download_bytes'])+'</td></tr>' for r in rows)+'</table>'
    text='<!doctype html><meta charset="utf-8"><meta http-equiv="refresh" content="5"><title>VPN 流量监控</title><style>body{font:15px/1.7 "Microsoft YaHei",sans-serif;max-width:1000px;margin:35px auto;padding:0 20px;color:#24334b}table{border-collapse:collapse;width:100%}td,th{padding:9px;border-bottom:1px solid #ddd;text-align:left}.note{color:#708095}</style>'
    text+='<h1>VPN 流量监控</h1><p>更新于 '+html.escape(now())+' · 每秒采样；页面每 5 秒刷新</p>'
    text+='<p id="stale" style="color:#b42318;display:none">监控数据已超过 20 秒未更新，请检查后台监控；这不代表 VPN 断开。</p>'
    text+='<script>const sampledAt='+json.dumps(local['observed_at'])+';function freshness(){document.getElementById("stale").style.display=Date.now()-Date.parse(sampledAt)>20000?"block":"none";}setInterval(freshness,1000);freshness();</script>'
    text+='<h2>本机：只监控，不自动停用</h2><p>代理内核累计 '+mb(local.get('payload_total_bytes'))+'；未归属／直连 '+mb(local.get('unattributed_or_direct_payload_bytes'))+'</p>'+table(local.get('proxy_application_rankings',[]))
    if local.get('monitor_error'):text+='<p class="note">本机监控接口暂不可用，以上是上次累计。</p>'
    if flows:
        text+='<h3>当前大流量连接</h3><table><tr><th>应用</th><th>目标域名／IP</th><th>连接累计下载</th></tr>'
        text+=''.join('<tr><td>'+html.escape(f['application'])+'</td><td>'+html.escape(f['destination'])+'</td><td>'+mb(f['download_bytes'])+'</td></tr>' for f in flows[:10])+'</table>'
    limit=server.get('limit_bytes') if server else None
    limit_label=f"{int(limit)/1000000000:g} GB 原始流量上限" if limit is not None else '只监控，不自动停用'
    text+='<h2>服务器：'+limit_label+'</h2>'
    if server:
        text+='<p>系统层累计 '+mb((server.get('kernel') or {}).get('used'))+(' / '+mb(limit) if limit is not None else ' · 无流量上限')+' · '+('已暂停 VPN' if server.get('paused') else '监控中')+'</p>'+table(server.get('proxy_application_rankings',[]))
        text+='<p class="note">服务器状态采集于 '+html.escape(server.get('observed_at','未知'))+'</p>'
    else:text+='<p>尚未获取服务器状态。</p>'
    text+='<p class="note">上传、下载均计入。应用排行是连接采样统计；短连接可能归入未归属项。代理内核字节与系统层字节口径不同，不能作为 VPN 服务商的精确账单。本机数据不计入服务器上限。</p>'
    tmp=target.with_suffix('.tmp');tmp.write_text(text,encoding='utf-8');replace_file(tmp,target)


def run(root, core_config, once=False):
    root.mkdir(parents=True,exist_ok=True)
    meter_path=root/'local-meter.json'
    meter=Meter(json.loads(meter_path.read_text())) if meter_path.exists() else Meter()
    opener=urllib.request.build_opener(urllib.request.ProxyHandler({}))
    server=None;last_server=0;last_history=0;flows=[]
    while True:
        error=False
        try:
            config=yaml.safe_load(core_config.read_text(encoding='utf-8'))
            controller=config.get('external-controller')
            if controller!='127.0.0.1:9090':raise ValueError('local_controller_not_enabled')
            headers={'Authorization':'Bearer '+config['secret']} if config.get('secret') else {}
            with opener.open(urllib.request.Request('http://127.0.0.1:9090/connections',headers=headers),timeout=2) as r:snapshot=json.load(r)
            meter.observe(snapshot)
            flows=[]
            for row in sorted(snapshot.get('connections') or [],key=lambda x:int(x.get('upload') or 0)+int(x.get('download') or 0),reverse=True):
                route=(row.get('chains') or ['DIRECT'])[0]
                if route in ('DIRECT','REJECT','REJECT-DROP','PASS'):continue
                meta=row.get('metadata') or {}
                flows.append({'application':str(meta.get('process') or 'unknown_process')[:160],
                    'destination':str(meta.get('host') or meta.get('destinationIP') or 'unknown')[:253],
                    'route':str(route)[:160],'started_at':row.get('start'),
                    'upload_bytes':int(row.get('upload') or 0),'download_bytes':int(row.get('download') or 0)})
                if len(flows)>=20:break
            atomic_json(root/'active-flows.json',{'observed_at':now(),'connections':flows})
            atomic_json(meter_path,meter.data)
        except (OSError,ValueError,KeyError,AttributeError,yaml.YAMLError) as exc:
            error=True
            record_error(root,'local_snapshot_failed',exc)
        local=meter.summary()|{'monitor_error':error,'automatic_cutoff':False}
        atomic_json(root/'local-status.json',local)
        if time.monotonic()-last_server>=10:
            last_server=time.monotonic()
            try:
                settings_path=root/'settings.json'
                settings=json.loads(settings_path.read_text()) if settings_path.exists() else {}
                target=settings.get('ssh_target')
                if target:
                    if not re.fullmatch(r'[a-zA-Z0-9_][a-zA-Z0-9_-]*@[a-zA-Z0-9][a-zA-Z0-9.:-]*',target):
                        raise ValueError('invalid_monitor_ssh_target')
                    result=subprocess.run(['ssh.exe','-o','BatchMode=yes','-o','ConnectTimeout=3',target,
                        'sudo -n cat /var/lib/codex-vpn-guard/status.json'],capture_output=True,timeout=5,creationflags=subprocess.CREATE_NO_WINDOW)
                    if result.returncode==0:
                        server=json.loads(result.stdout);atomic_json(root/'server-status.json',server)
            except (OSError,ValueError,subprocess.SubprocessError) as exc:record_error(root,'server_snapshot_failed',exc)
        if time.monotonic()-last_history>=60:
            last_history=time.monotonic()
            history=root/'history';history.mkdir(exist_ok=True)
            with (history/(now()[:10]+'.jsonl')).open('a',encoding='utf-8') as stream:
                stream.write(json.dumps({'at':now(),'local':local,'server':server,'active_flows':flows},ensure_ascii=True)+'\n')
            for old in sorted(history.glob('*.jsonl'))[:-30]:old.unlink()
        try:render(root/'VPN流量监控.html',local,server,flows)
        except OSError as exc:record_error(root,'report_write_failed',exc)
        if once:return
        time.sleep(1)


def main():
    parser=argparse.ArgumentParser();parser.add_argument('--once',action='store_true');args=parser.parse_args()
    root=Path(os.environ['LOCALAPPDATA'])/'CodexRelay/vpn-monitor'
    root.mkdir(parents=True,exist_ok=True)
    with (root/'monitor.lock').open('a+b') as lock:
        lock.seek(0);lock.write(b'0');lock.flush();lock.seek(0)
        try:msvcrt.locking(lock.fileno(),msvcrt.LK_NBLCK,1)
        except OSError:return
        atomic_json(root/'monitor-process.json',{'pid':os.getpid(),'started_at':now()})
        while True:
            try:
                run(root,Path(os.environ['APPDATA'])/'com.follow/clash/config.yaml',args.once)
                return
            except Exception as exc:
                record_error(root,'monitor_loop_recovered',exc)
                if args.once:raise
                time.sleep(2)


if __name__=='__main__':main()
