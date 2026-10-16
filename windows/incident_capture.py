"""Opt-in private exception snapshots, captured before SSE error sanitization."""
import asyncio
from collections import deque
import gzip
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import threading
import time
import uuid

import anyio
import httpx

from .runtime_diagnostics import exception_info, stamp
from .sse import MAX_EVENT

RESPONSE_LIMIT=8*1024*1024
EVENT_LIMIT=256*1024
RECORD_LIMIT=20
DISK_LIMIT=256*1024*1024
OWNED=re.compile(r'\d{20}-[0-9a-f]{32}')


class BodyBuffer:
    """Keep exact bytes, or bounded head/tail with an explicit missing interval."""
    def __init__(self,limit):
        self.limit=limit;self.head=bytearray();self.tail=deque();self.tail_size=0
        self.total=0;self.digest=hashlib.sha256()

    def add(self,chunk):
        self.total+=len(chunk);self.digest.update(chunk)
        take=min(len(chunk),self.limit//2-len(self.head))
        self.head.extend(chunk[:take]);remaining=chunk[take:]
        if remaining:self.tail.append(remaining);self.tail_size+=len(remaining)
        excess=self.tail_size-(self.limit-self.limit//2)
        while excess>0:
            first=self.tail.popleft();removed=min(excess,len(first));self.tail_size-=removed;excess-=removed
            if removed<len(first):self.tail.appendleft(first[removed:])

    def info(self):
        return {'received_bytes':self.total,'captured_bytes':len(self.head)+self.tail_size,
                'truncated':self.total>len(self.head)+self.tail_size,
                'missing_middle_bytes':self.total-len(self.head)-self.tail_size,
                'sha256':self.digest.hexdigest()}


class IncidentStore:
    def __init__(self,config,diagnostics):
        self.enabled=config.adapter.incident_capture_bodies
        self.root=config.runtime_dir.parent/'logs/incidents'
        self.request_limit=config.bridge.max_body_bytes+65536
        self.diagnostics=diagnostics;self.lock=threading.Lock();self.write_error=False

    def begin(self,body,request_id,record_id):
        if not self.enabled:return None
        return Incident(self,body,request_id,record_id)

    @staticmethod
    def write_private(path,raw,compressed=False):
        fd=os.open(path,os.O_WRONLY|os.O_CREAT|os.O_EXCL,0o600)
        with os.fdopen(fd,'wb') as stream:
            if compressed:
                with gzip.GzipFile(filename='',fileobj=stream,mode='wb',compresslevel=1,mtime=0) as zipped:zipped.write(raw)
            else:stream.write(raw)

    def remove_owned(self,path):
        # Validate the absolute target before recursive removal on either OS.
        if OWNED.fullmatch(path.name) and not path.is_symlink() and path.resolve().parent==self.root.resolve():
            shutil.rmtree(path)

    def save(self,incident):
        with self.lock:
            self.root.mkdir(parents=True,exist_ok=True,mode=0o700)
            if os.name!='nt':self.root.chmod(0o700)
            ident=f'{time.time_ns():020d}-{uuid.uuid4().hex}'
            target=self.root/ident;target.mkdir(mode=0o700)
            try:
                metadata={'version':1,'created_at':stamp(),'request_id':incident.request_id,
                          'cache_record_id':incident.record_id,'reasons':incident.reasons,
                          'exception':incident.exception,'request_attempts':incident.attempt_no,
                          'requests':{},'responses':[],'error_events':[]}
                for name,raw in [('client',incident.client_body),('upstream',incident.sent_body)]:
                    if raw is None:continue
                    filename=name+'.request.json.gz';captured=raw[:self.request_limit]
                    self.write_private(target/filename,captured,True)
                    metadata['requests'][name]={'file':filename,'body_bytes':len(raw),'captured_bytes':len(captured),
                                               'truncated':len(raw)>len(captured),'sha256':hashlib.sha256(raw).hexdigest()}
                for number,attempt in enumerate(incident.attempts,1):
                    buffer=attempt['buffer'];info=buffer.info();head=bytes(buffer.head);tail=b''.join(buffer.tail)
                    item={'attempt':attempt['number'],'http_status':attempt['status'],
                          'content_type':attempt['content_type'],'content_encoding':attempt['content_encoding'],**info}
                    if info['truncated']:
                        files={'head':f'{number}.response.head.gz','tail':f'{number}.response.tail.gz'}
                        self.write_private(target/files['head'],head,True);self.write_private(target/files['tail'],tail,True)
                    else:
                        files={'body':f'{number}.response.body.gz'};self.write_private(target/files['body'],head+tail,True)
                    item['files']=files;metadata['responses'].append(item)
                for number,event in enumerate(incident.events,1):
                    filename=f'{number}.error-event.sse.gz';self.write_private(target/filename,event['raw'][:EVENT_LIMIT],True)
                    metadata['error_events'].append({'file':filename,'type':event['type'],'bytes':len(event['raw']),
                                                     'truncated':len(event['raw'])>EVENT_LIMIT})
                self.write_private(target/'metadata.json',json.dumps(metadata,ensure_ascii=False,indent=2).encode())
                # A marker identifies complete snapshots; incomplete writes are not advertised.
                self.write_private(target/'complete',b'')
                folders=sorted(p for p in self.root.iterdir() if OWNED.fullmatch(p.name) and p.is_dir() and not p.is_symlink())
                sizes={p:sum(f.stat().st_size for f in p.iterdir() if f.is_file()) for p in folders}
                total=sum(sizes.values())
                while folders and (len(folders)>RECORD_LIMIT or total>DISK_LIMIT):
                    oldest=folders.pop(0);total-=sizes[oldest];self.remove_owned(oldest)
                self.write_error=False
                self.diagnostics.emit('incident_captured',request_id=incident.request_id,cache_record_id=incident.record_id,incident_id=ident)
            except Exception:
                self.remove_owned(target)
                raise


class Incident:
    def __init__(self,store,body,request_id,record_id):
        self.store=store;self.client_body=body;self.sent_body=None;self.record_id=record_id
        try:self.request_id=str(uuid.UUID(request_id))
        except (ValueError,TypeError,AttributeError):self.request_id=None
        self.attempts=[];self.events=[];self.reasons=[];self.exception=None;self.saved=False;self.attempt_no=0
        self.finish_lock=asyncio.Lock()

    def flag(self,reason):
        if reason not in self.reasons:self.reasons.append(reason)

    def sent(self,body):
        self.sent_body=body;self.attempt_no+=1

    def failed(self,exc):
        if isinstance(exc,(asyncio.CancelledError,GeneratorExit)):return
        self.exception=exception_info(exc)
        self.flag('protocol_or_assembly_error' if isinstance(exc,ValueError) else 'transport_error')

    async def response(self,response):
        attempt={'number':self.attempt_no,'status':response.status_code,'buffer':BodyBuffer(RESPONSE_LIMIT),
                 'content_type':response.headers.get('content-type','')[:160],
                 'content_encoding':response.headers.get('content-encoding','identity')[:64]}
        self.attempts.append(attempt)
        if response.status_code!=200:self.flag('upstream_http_error')
        elif attempt['content_encoding'].lower() not in ('','identity'):self.flag('unexpected_content_encoding')
        elif attempt['content_type'] and not attempt['content_type'].lower().startswith('text/event-stream'):
            self.flag('unexpected_content_type')
        response.stream=CapturedStream(response.stream,self,attempt)
        # In-memory transports can return a body that has already been consumed.
        if response.is_stream_consumed:response.stream.record(response.content)

    async def finish(self):
        if not self.reasons or self.saved:return
        # Snapshot failure must never mask the real request result or cause a retry.
        with anyio.CancelScope(shield=True):
            async with self.finish_lock:
                if self.saved:return
                self.saved=True
                try:await asyncio.to_thread(self.store.save,self)
                except Exception as exc:
                    self.store.write_error=True
                    self.store.diagnostics.emit('incident_capture_failed',request_id=self.request_id,
                                                cache_record_id=self.record_id,**exception_info(exc))


class CapturedStream(httpx.AsyncByteStream):
    def __init__(self,source,incident,attempt):
        self.source=source;self.incident=incident;self.attempt=attempt
        self.pending=bytearray();self.scanner_disabled=False

    def observe(self,chunk):
        self.attempt['buffer'].add(chunk)
        if self.attempt['status']!=200 or self.scanner_disabled:return
        self.pending.extend(chunk)
        while (match:=re.search(br'\r?\n\r?\n',self.pending)):
            end=match.end()
            if end>MAX_EVENT:self.scanner_disabled=True;self.pending.clear();return
            frame=bytes(self.pending[:end]);del self.pending[:end]
            try:
                lines=frame.decode('utf-8').splitlines()
                event=next((line[6:].strip() for line in lines if line.startswith('event:')),'')
                data='\n'.join(line[5:].lstrip(' ') for line in lines if line.startswith('data:'))
                obj=json.loads(data);kind=obj.get('type',event) if isinstance(obj,dict) else event
            except (ValueError,UnicodeError):continue
            if kind in {'error','response.failed','response.incomplete'}:
                self.incident.flag('upstream_'+kind)
                if len(self.incident.events)<8:self.incident.events.append({'type':kind,'raw':frame})
        if len(self.pending)>MAX_EVENT:self.scanner_disabled=True;self.pending.clear()

    def record(self,chunk):
        try:self.observe(chunk)
        except Exception as exc:
            self.scanner_disabled=True;self.pending.clear();self.incident.store.write_error=True
            self.incident.store.diagnostics.emit('incident_capture_failed',request_id=self.incident.request_id,
                                                cache_record_id=self.incident.record_id,**exception_info(exc))

    async def __aiter__(self):
        try:
            async for chunk in self.source:
                self.record(chunk)
                yield chunk
        except BaseException as exc:
            self.incident.failed(exc)
            raise

    async def aclose(self):await self.source.aclose()
