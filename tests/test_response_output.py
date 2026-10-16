import json

import httpx
import pytest

from tests.test_single_login import single
from windows.existing_adapter import ExistingAdapter
from windows.response_output import CompleteOutputStream, event_object
from windows.sse import SSEFramer


def message(ident,text,phase='final_answer'):
    return {'id':ident,'type':'message','role':'assistant','phase':phase,'status':'completed',
            'content':[{'type':'output_text','text':text,'annotations':[]}]}


def terminal(output):
    return {'type':'response.completed','response':{'id':'resp_mock','status':'completed',
        'model':'mock-codex','output':output,'usage':{'input_tokens':12576,'output_tokens':739,
        'input_tokens_details':{'cached_tokens':0},'total_tokens':13315}}}


def sse(events):return ''.join('data: '+json.dumps(x,ensure_ascii=False)+'\n\n' for x in events).encode()


class Split(httpx.AsyncByteStream):
    def __init__(self,raw):self.raw=raw
    async def __aiter__(self):
        for i in range(0,len(self.raw),7):yield self.raw[i:i+7]
    async def aclose(self):pass


@pytest.mark.parametrize('stream',[False,True])
@pytest.mark.parametrize('sparse',[[],[message('final','')]])
async def test_long_image_request_and_sparse_output_both_modes(config,state,tmp_path,stream,sparse):
    single(config,tmp_path)
    visible=message('final','最终动作 '+('x'*2761))
    reasoning={'id':'reason','type':'reasoning','encrypted_content':'opaque-test','summary':[]}
    events=[{'type':'response.output_item.done','output_index':0,'item':reasoning},
            {'type':'response.output_item.done','output_index':1,'item':visible},terminal(sparse)]
    def handler(request):
        sent=json.loads(request.content)
        assert len(sent['instructions'])==18455
        assert sent['input'][0]['content'][1]['image_url'].startswith('data:image/jpeg;base64,')
        return httpx.Response(200,stream=Split(sse(events)))
    adapter=ExistingAdapter(config,state,transport=httpx.MockTransport(handler))
    body={'model':'mock-codex','instructions':'x'*18455,'stream':stream,'input':[{'role':'user','content':[
        {'type':'input_text','text':'fixed'}, {'type':'input_image','image_url':'data:image/jpeg;base64,'+'YQ=='*27535}]}]}
    try:
        response=await adapter.open('POST','/v1/responses',json.dumps(body).encode())
        if stream:
            parser=SSEFramer();objects=[event_object(f) for f in parser.feed(await response.aread())]
            result=next(x['response'] for x in objects if x and x.get('type')=='response.completed')
        else:result=response.json()
        assert result['output']==[reasoning,visible]
        assert result['usage']['output_tokens']==739
        await response.aclose()
        row=adapter.cache_log.rows[-1]
        assert row['output_repaired'] is True
        assert row['downstream_visible_messages'][0]['characters']==len(visible['content'][0]['text'])
    finally:await adapter.close()


async def test_content_done_repairs_blank_placeholder_without_reclassifying():
    commentary=message('comment','explanation','commentary');blank=message('final','')
    events=[{'type':'response.output_item.done','output_index':0,'item':commentary},
        {'type':'response.output_item.added','output_index':1,'item':blank},
        {'type':'response.output_text.delta','output_index':1,'content_index':0,'delta':'动作'},
        {'type':'response.output_text.done','output_index':1,'content_index':0,'text':'动作'},terminal([commentary,blank])]
    frames=SSEFramer().feed(b''.join([x async for x in CompleteOutputStream(Split(sse(events)),100000)]))
    result=event_object(frames[-1])['response']['output']
    assert result[0]==commentary and result[1]['phase']=='final_answer'
    assert result[1]['content'][0]['text']=='动作'


async def test_genuinely_empty_final_is_preserved():
    original=[message('comment','explanation','commentary'),message('final','')]
    events=[{'type':'response.output_item.done','output_index':i,'item':v} for i,v in enumerate(original)]+[terminal(original)]
    raw=sse(events)
    assert b''.join([x async for x in CompleteOutputStream(Split(raw),100000)])==raw


async def test_conflicting_terminal_never_exposes_success():
    events=[terminal([message('final','one')]),terminal([message('final','two')])]
    received=[]
    with pytest.raises(ValueError,match='conflicting_terminal'):
        async for x in CompleteOutputStream(Split(sse(events)),100000):received.append(x)
    assert b'response.completed' not in b''.join(received)


async def test_duplicate_terminal_emitted_once():
    event=terminal([message('final','one')]);raw=sse([event,event])
    result=b''.join([x async for x in CompleteOutputStream(Split(raw),100000)])
    assert result==sse([event])


async def test_delta_without_terminal_cannot_be_success():
    events=[{'type':'response.output_text.delta','output_index':0,'content_index':0,'delta':'partial'}]
    with pytest.raises(ValueError,match='missing_terminal'):
        async for _ in CompleteOutputStream(Split(sse(events)),100000):pass
