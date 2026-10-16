"""Actual loopback TCP tests: early delivery, disconnect, idle/total bounds."""
import asyncio
import socket
import time
from contextlib import asynccontextmanager

import httpx
import pytest
import uvicorn
from fastapi import FastAPI
from starlette.responses import StreamingResponse

from windows.bridge import create_app
from windows.config import read_key
from windows.proxy_adapter import ProxyAdapter


@asynccontextmanager
async def serve(app):
    sock = socket.socket()
    sock.bind(("127.0.0.1", 0))
    sock.listen(128)
    sock.setblocking(False)
    port = sock.getsockname()[1]
    server = uvicorn.Server(uvicorn.Config(app, log_config=None, access_log=False,
                                          proxy_headers=False, timeout_graceful_shutdown=2))
    server.install_signal_handlers = lambda: None
    task = asyncio.create_task(server.serve(sockets=[sock]))
    try:
        for _ in range(100):
            if server.started:
                break
            await asyncio.sleep(.01)
        assert server.started
        yield f"http://127.0.0.1:{port}"
    finally:
        server.should_exit = True
        await asyncio.wait_for(task, 4)
        sock.close()


async def test_early_sse_delivery_and_disconnect_cleanup(config, state):
    upstream = FastAPI()
    closed = asyncio.Event()

    @upstream.post("/v1/responses")
    async def endpoint():
        async def events():
            try:
                yield b'event: response.output_text.delta\ndata: {"type":"response.output_text.delta","delta":"one"}\n\n'
                await asyncio.sleep(10)
            finally:
                closed.set()
        return StreamingResponse(events(), media_type="text/event-stream")

    async with serve(upstream) as up_url:
        adapter = ProxyAdapter(config, state)
        # Test-only injection: production configuration still forbids arbitrary URLs.
        adapter.models.base_url = httpx.URL(up_url)
        async with serve(create_app(config, state, adapter)) as bridge_url:
            async with httpx.AsyncClient(base_url=bridge_url, trust_env=False, timeout=2,
                headers={"Authorization": "Bearer " + read_key(config.bridge.auth_secret_file, "wkr_")}) as client:
                start = time.monotonic()
                async with client.stream("POST", "/v1/responses", json={"model": "mock-codex", "stream": True}) as response:
                    first = await response.aiter_bytes().__anext__()
                    assert b'"delta":"one"' in first and time.monotonic() - start < 1
                    assert state.active_requests == 1
                await asyncio.wait_for(closed.wait(), 2)
                for _ in range(100):
                    if state.active_requests == 0:
                        break
                    await asyncio.sleep(.01)
                assert state.active_requests == 0 and state.last_inference_success_at is None


@pytest.mark.parametrize("kind", ["idle", "total"])
async def test_started_stream_timeout_closes_incomplete(config, state, kind):
    upstream = FastAPI()
    config.bridge.idle_seconds = .05 if kind == "idle" else 2
    config.bridge.total_seconds = .08 if kind == "total" else 2

    @upstream.post("/v1/responses")
    async def endpoint():
        async def events():
            yield b'event: response.output_text.delta\ndata: {"type":"response.output_text.delta","delta":"one"}\n\n'
            await asyncio.sleep(10)
        return StreamingResponse(events(), media_type="text/event-stream")

    async with serve(upstream) as up_url:
        adapter = ProxyAdapter(config, state)
        adapter.models.base_url = httpx.URL(up_url)
        async with serve(create_app(config, state, adapter)) as bridge_url:
            async with httpx.AsyncClient(base_url=bridge_url, trust_env=False, timeout=2,
                headers={"Authorization": "Bearer " + read_key(config.bridge.auth_secret_file, "wkr_")}) as client:
                received = bytearray()
                with pytest.raises(httpx.RemoteProtocolError):
                    async with client.stream("POST", "/v1/responses", json={"model": "mock-codex", "stream": True}) as response:
                        assert response.status_code == 200
                        async for chunk in response.aiter_bytes():
                            received.extend(chunk)
                assert b"response.completed" not in received
                await asyncio.sleep(.05)
                assert state.active_requests == 0
