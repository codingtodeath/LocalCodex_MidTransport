"""Portable, credential-free client probe. Run on the device having trouble."""
import argparse
import json
import queue
import socket
import ssl
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid
from datetime import datetime, timezone
from pathlib import Path


def classify(exc):
    if isinstance(exc, (TimeoutError, socket.timeout)): return "timeout"
    if isinstance(exc, ssl.SSLCertVerificationError): return "certificate_verification_failed"
    if isinstance(exc, ssl.SSLError): return "tls_failed"
    if isinstance(exc, socket.gaierror): return "dns_failed"
    if isinstance(exc, ConnectionRefusedError): return "connection_refused"
    if isinstance(exc, urllib.error.URLError): return classify(exc.reason) if isinstance(exc.reason, BaseException) else "http_transport_failed"
    return "connection_failed"


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *args, **kwargs): return None


def probe(base_url, mode="direct"):
    u=urllib.parse.urlsplit(base_url)
    if u.scheme!="https" or not u.hostname or u.username or u.password or u.query or u.fragment:
        raise ValueError("use_https_url_without_credentials_or_query")
    port=u.port or 443
    origin=urllib.parse.urlunsplit((u.scheme,u.netloc,"","",""))
    report={"version":1,"at":datetime.now(timezone.utc).isoformat(),"target":origin,"http_mode":mode,"phases":[]}
    def measure(name, fn):
        started=time.monotonic();r={"phase":name}
        try:r.update(fn());r["ok"]=True
        except Exception as exc:r.update(ok=False,error_code=classify(exc),error_type=type(exc).__name__[:60])
        r["duration_ms"]=round((time.monotonic()-started)*1000);report["phases"].append(r)
    def dns():
        q=queue.Queue()
        def lookup():
            try:q.put(socket.getaddrinfo(u.hostname,port,type=socket.SOCK_STREAM))
            except Exception as exc:q.put(exc)
        threading.Thread(target=lookup,daemon=True).start()
        try:v=q.get(timeout=5)
        except queue.Empty:raise TimeoutError() from None
        if isinstance(v,Exception):raise v
        return {"resolved_address_count":len({x[4][0] for x in v})}
    def tcp():
        with socket.create_connection((u.hostname,port),timeout=5):return {}
    def tls():
        with socket.create_connection((u.hostname,port),timeout=5) as sock:
            with ssl.create_default_context().wrap_socket(sock,server_hostname=u.hostname) as conn:
                return {"tls_protocol":conn.version()}
    def http():
        handler=urllib.request.ProxyHandler({}) if mode=="direct" else urllib.request.ProxyHandler()
        opener=urllib.request.build_opener(handler,NoRedirect())
        response=None
        try:
            try:response=opener.open(urllib.request.Request(origin+'/v1/models',headers={'User-Agent':'codex-relay-connection-probe/1'}),timeout=10)
            except urllib.error.HTTPError as exc:response=exc
            r={"http_status":response.code,"server_http_reached":True,"expected_unauthenticated_status":response.code==401}
            value=response.headers.get('X-Request-ID','')
            try:r['request_id']=str(uuid.UUID(value))
            except ValueError:pass
            return r
        finally:
            if response is not None:response.close()
    measure("dns_direct",dns);measure("tcp_direct",tcp);measure("tls_direct",tls);measure("http_"+mode,http)
    return report


def main():
    p=argparse.ArgumentParser(description=__doc__);p.add_argument('--base-url',required=True)
    p.add_argument('--mode',choices=['direct','system'],default='direct');p.add_argument('--output',type=Path,default=Path('client-connection-report.json'))
    args=p.parse_args()
    try:r=probe(args.base_url,args.mode)
    except ValueError:print('{"error":"invalid_https_base_url"}');return 2
    args.output.write_text(json.dumps(r,indent=2),encoding='utf-8');print(json.dumps(r,indent=2));return 0


if __name__=='__main__':raise SystemExit(main())
