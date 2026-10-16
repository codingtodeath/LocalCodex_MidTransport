"""Complete sparse Responses terminal output from finished upstream items.

Preserve phases and tool / reasoning items. Never turn commentary into a final
answer, and never manufacture success from a text delta or an incomplete EOF.
"""
import copy
import hashlib
import json

import httpx

from .sse import SSEFramer


TERMINALS={'response.completed','response.failed','response.incomplete'}


def event_object(frame):
    data='\n'.join(line[5:].lstrip(' ') for line in frame.decode('utf-8').splitlines() if line.startswith('data:'))
    try:return json.loads(data)
    except ValueError:return None


class OutputAssembly:
    def __init__(self):
        self.items={};self.done={};self.parts={};self.text={};self.deltas={}
        self.repaired=False

    def observe(self,obj):
        kind=obj.get('type');index=obj.get('output_index')
        if type(index) is not int or not 0<=index<4096:return
        if kind in ('response.output_item.added','response.output_item.done'):
            item=obj.get('item')
            if not isinstance(item,dict):raise ValueError('invalid_output_item')
            if kind.endswith('.done'):
                if index in self.done and self.done[index]!=item:raise ValueError('conflicting_output_item')
                self.done[index]=copy.deepcopy(item)
            else:self.items[index]=copy.deepcopy(item)
        part_index=obj.get('content_index')
        if type(part_index) is not int or not 0<=part_index<4096:return
        key=(index,part_index)
        if kind=='response.content_part.done' and isinstance(obj.get('part'),dict):
            self.parts[key]=copy.deepcopy(obj['part'])
        elif kind=='response.output_text.done' and isinstance(obj.get('text'),str):
            self.text[key]=obj['text']
        elif kind=='response.output_text.delta' and isinstance(obj.get('delta'),str):
            self.deltas.setdefault(key,[]).append(obj['delta'])

    def finish_item(self,item,index):
        result=copy.deepcopy(item)
        if result.get('type')!='message':return result
        content=result.get('content') or []
        if not isinstance(content,list):raise ValueError('invalid_output_content')
        evidence={p:copy.deepcopy(v) for (i,p),v in self.parts.items() if i==index}
        for (i,p),text in self.text.items():
            if i==index:
                part=evidence.setdefault(p,{'type':'output_text','annotations':[]})
                if part.get('text') and part['text']!=text:raise ValueError('conflicting_output_text')
                delta=''.join(self.deltas.get((i,p),[]))
                if delta and not text.startswith(delta):raise ValueError('conflicting_output_text')
                part['text']=text
        for p,part in evidence.items():
            if p>len(content):raise ValueError('missing_output_content_part')
            if p==len(content):content.append(part)
            else:content[p]=self.merge_part(content[p],part)
        result['content']=content
        return result

    @staticmethod
    def merge_part(old,new):
        if not isinstance(old,dict) or not isinstance(new,dict):raise ValueError('invalid_output_content')
        result=copy.deepcopy(old)
        for field in ('type','text','refusal'):
            a,b=old.get(field),new.get(field)
            if a and b and a!=b:raise ValueError('conflicting_output_text')
        for k,v in new.items():
            if k not in result or result[k] in (None,'',[]):result[k]=copy.deepcopy(v)
        return result

    def merge_item(self,old,new,index):
        result=copy.deepcopy(old)
        for k in ('id','type','phase','role','call_id','name','arguments'):
            a,b=old.get(k),new.get(k)
            if a and b and a!=b:raise ValueError('conflicting_output_item')
        for k,v in new.items():
            if k not in result or result[k] in (None,'',[]):result[k]=copy.deepcopy(v)
        if old.get('type')=='message' and old.get('content') and new.get('content'):
            a,b=result['content'],new['content']
            for i,part in enumerate(b):
                if i==len(a):a.append(copy.deepcopy(part))
                elif i<len(a):a[i]=self.merge_part(a[i],part)
        return self.finish_item(result,index)

    def complete(self,response):
        if not isinstance(response,dict):raise ValueError('invalid_terminal')
        original=response.get('output')
        if original is not None and not isinstance(original,list):raise ValueError('invalid_terminal_output')
        finished={i:self.finish_item(v,i) for i,v in self.done.items()}
        # A completed text/content-part event can finish an added message too.
        if response.get('status')=='completed':
            for index,item in self.items.items():
                if index not in finished and item.get('type')=='message' and any(i==index for i,_ in (*self.parts,*self.text)):
                    finished[index]=self.finish_item(item,index)
                    finished[index]['status']='completed'
        by_id={item.get('id'):i for i,item in finished.items() if item.get('id')}
        merged={}
        for position,item in enumerate(original or []):
            if not isinstance(item,dict):raise ValueError('invalid_output_item')
            index=by_id.get(item.get('id'),position)
            if index in merged:raise ValueError('conflicting_output_item')
            if index in finished:item=self.merge_item(item,finished[index],index)
            else:item=self.finish_item(item,index)
            merged[index]=item
        for i,item in finished.items():
            if i not in merged:merged[i]=item
        output=[merged[i] for i in sorted(merged)]
        result=copy.deepcopy(response)
        if output or original is not None:result['output']=output
        self.repaired=result!=response
        return result

    @staticmethod
    def visible_summary(response):
        result=[]
        for item in (response.get('output') or [])[:32]:
            if not isinstance(item,dict) or item.get('type')!='message':continue
            text=''.join(p.get('text','') for p in item.get('content',[]) if isinstance(p,dict) and p.get('type')=='output_text')
            result.append({'phase':item.get('phase') if item.get('phase') in ('commentary','final_answer','partial_answer') else None,
                           'status':item.get('status') if item.get('status') in ('completed','incomplete','in_progress') else None,'characters':len(text),
                           'sha256':hashlib.sha256(text.encode()).hexdigest()})
        return result


class CompleteOutputStream(httpx.AsyncByteStream):
    def __init__(self,source,limit,log=None,record_id=None,incident=None):
        self.source,self.limit,self.log,self.record_id=source,limit,log,record_id
        self.incident=incident

    async def __aiter__(self):
        framer=SSEFramer();assembly=OutputAssembly();terminal=None;trailer=[];size=0
        try:
            async for chunk in self.source:
                size+=len(chunk)
                if size>self.limit:raise ValueError('response_too_large')
                for frame in framer.feed(chunk):
                    obj=event_object(frame)
                    if not isinstance(obj,dict):
                        if terminal is None:yield frame
                        else:trailer.append(frame)
                        continue
                    kind=obj.get('type')
                    if kind in TERMINALS:
                        response=assembly.complete(obj.get('response'))
                        normalized=dict(obj,response=response)
                        if terminal is not None:
                            if terminal[0]!=normalized:raise ValueError('conflicting_terminal')
                            continue
                        if assembly.repaired:
                            prefix='\n'.join(x for x in frame.decode().splitlines() if not x.startswith('data:') and x)
                            frame=((prefix+'\n' if prefix else '')+'data: '+json.dumps(normalized,ensure_ascii=False)+'\n\n').encode()
                        terminal=(normalized,frame)
                        if self.log:
                            await self.log.update(self.record_id,output_repaired=assembly.repaired,
                                upstream_visible_messages=assembly.visible_summary(obj['response']),
                                downstream_visible_messages=assembly.visible_summary(response),
                                empty_final_answer=any(v.get('phase')=='final_answer' and v['characters']==0 for v in assembly.visible_summary(response)))
                    elif terminal is not None:
                        raise ValueError('event_after_terminal')
                    else:
                        assembly.observe(obj)
                        yield frame
            if framer.buffer or terminal is None:raise ValueError('missing_terminal')
            # Hold just the terminal until clean EOF: conflicting endings must
            # never be exposed as successful or billed as a completed response.
            yield terminal[1]
            for frame in trailer:yield frame
        except ValueError as exc:
            if self.incident:self.incident.failed(exc)
            if self.log:
                await self.log.update(self.record_id,response_assembly_error=str(exc) if str(exc) in {
                    'invalid_output_item','conflicting_output_item','invalid_output_content','conflicting_output_text',
                    'missing_output_content_part','invalid_terminal','invalid_terminal_output','conflicting_terminal',
                    'event_after_terminal','missing_terminal','response_too_large'} else 'invalid_response')
            raise
        except BaseException as exc:
            if self.incident:self.incident.failed(exc)
            raise
        finally:
            if self.incident:await self.incident.finish()

    async def aclose(self):
        try:await self.source.aclose()
        finally:
            if self.incident:await self.incident.finish()
