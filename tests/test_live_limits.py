import asyncio
import json
from types import SimpleNamespace

import httpx

from codex_relay.services import ConcurrencyGate
from codex_relay.config import RelayConfig
from linux_worker.gateway import LiveAdmission
from linux_worker.live_limits import apply_gateway_limits, apply_worker_limits, read_limits


def test_raise_limits_preserves_active_slots_and_releases(tmp_path):
    gate = ConcurrencyGate(3, 3)
    services = SimpleNamespace(gate=gate, config=RelayConfig(max_concurrency=3, per_client_concurrency=3))
    for _ in range(3):
        assert gate.try_acquire('same-key') is None
    assert gate.try_acquire('same-key') == 'gateway_concurrency_limit'
    path = tmp_path/'limits.json'
    path.write_text(json.dumps({'global_limit':4,'per_client_limit':4}))
    apply_gateway_limits(services, path)
    assert gate.global_active == 3 and gate.active_for('same-key') == 3
    assert gate.try_acquire('same-key') is None
    assert gate.try_acquire('another-key') == 'gateway_concurrency_limit'
    for _ in range(4):gate.release('same-key')
    assert gate.global_active == 0
    worker = SimpleNamespace(bridge=SimpleNamespace(max_concurrency=3))
    apply_worker_limits(worker, path)
    assert worker.bridge.max_concurrency == 4
    path.write_text('{')
    apply_gateway_limits(services,path)
    apply_worker_limits(worker,path)
    assert gate.global_limit == worker.bridge.max_concurrency == 4


def test_invalid_limits_do_not_apply(tmp_path):
    path=tmp_path/'limits.json'
    for values in [(True,1),(0,0),(4,5),(101,1),(4,'4')]:
        path.write_text(json.dumps(dict(zip(['global_limit','per_client_limit'],values))))
        assert read_limits(path) is None


async def test_admission_pause_keeps_existing_stream_and_request_body(tmp_path):
    path=tmp_path/'pause'
    opened=asyncio.Event();release=asyncio.Event();seen=[]
    async def app(scope,receive,send):
        chunk=await receive();seen.append(chunk.get('body'))
        await send({'type':'http.response.start','status':200,'headers':[]})
        if scope['path']=='/v1/responses' and chunk.get('body')==b'first':
            opened.set();await release.wait()
        await send({'type':'http.response.body','body':chunk.get('body',b'')})
    services=SimpleNamespace(gate=ConcurrencyGate(3,3),config=RelayConfig(max_concurrency=3, per_client_concurrency=3))
    wrapped=LiveAdmission(app,services,path)
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=wrapped),base_url='http://test') as client:
        first=asyncio.create_task(client.post('/v1/responses',content=b'first'))
        await opened.wait()
        path.touch()
        second=asyncio.create_task(client.post('/v1/responses',content=b'full input and tools'))
        await asyncio.sleep(.08)
        assert seen==[b'first'] and not second.done()
        assert (await client.get('/healthz')).status_code==200
        release.set();assert (await first).content==b'first'
        path.unlink();assert (await second).content==b'full input and tools'
