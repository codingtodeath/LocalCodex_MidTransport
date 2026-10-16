import asyncio
import json
from pathlib import Path

import httpx
import pytest

from windows.bridge import create_app
from windows.config import read_key
from windows.contracts import validate
from windows.proxy_adapter import ProxyAdapter

ROOT = Path(__file__).resolve().parents[1]
CANARY = "SYNTHETIC_CANARY_NEVER_LOG"


class BytesStream(httpx.AsyncByteStream):
    def __init__(self, data, delay=0):
        self.data, self.delay, self.closed = data, delay, False

    async def __aiter__(self):
        for chunk in self.data:
            if self.delay:
                await asyncio.sleep(self.delay)
            yield chunk

    async def aclose(self):
        self.closed = True


async def client(config, state, handler):
    adapter = ProxyAdapter(config, state, transport=httpx.MockTransport(handler))
    app = create_app(config, state, adapter)
    return httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://bridge",
                            headers={"Authorization": "Bearer " + read_key(config.bridge.auth_secret_file, "wkr_")}), adapter


@pytest.mark.parametrize("path", ["/v0/management", "/v8/management/auth-files", "/auth-files", "/app-server", "/shell", "/docs", "/openapi.json", "/proxy?url=http://evil", "/v1//responses", "/%76%31/responses", "/v1/%2e%2e/management", "/v1/%252e%252e/responses", "/v1/%5cresponses", "/v1/responses/", "/v1/responses?url=http://evil"])
async def test_path_allowlist(config, state, path):
    calls = []
    c, a = await client(config, state, lambda r: calls.append(r))
    async with c:
        r = await c.post(path, json={"model": "mock-codex"})
    await a.close()
    assert r.status_code == 404
    assert not calls


async def test_roles_methods_and_health(config, state):
    c, a = await client(config, state, lambda r: httpx.Response(200, json={"data": []}))
    async with c:
        for key in ("", "gw_" + "a" * 43, "tel_" + "a" * 43):
            assert (await c.get("/internal/v1/health", headers={"Authorization": "Bearer " + key})).status_code == 401
        assert (await c.get("/v1/responses")).status_code == 405
        r = await c.get("/internal/v1/health")
        validate("health", r.json())
        assert r.json()["instance_id"] == state.instance_id
        assert r.json()["local"]["auth_state"] == "unknown"
    await a.close()


async def test_transparency_and_header_separation(config, state):
    raw = json.dumps({"model": "mock-codex", "stream": False, "tools": [{"type": "function", "name": "test"}],
        "input": [{"type": "function_call_output", "call_id": "a", "output": "中文"}],
        "reasoning": {"effort": "high"}, "encrypted_reasoning_content": "opaque", "future": {"value": 1}}, ensure_ascii=False).encode()
    seen = []
    def handler(r):
        seen.append(r)
        return httpx.Response(200, json={"id": "resp_mock", "status": "completed", "usage": {"input_tokens": 0}},
                              headers={"Set-Cookie": CANARY, "Authorization": CANARY})
    c, a = await client(config, state, handler)
    async with c:
        r = await c.post("/v1/responses", content=raw, headers={"Content-Type": "application/json",
            "Cookie": CANARY, "Forwarded": CANARY, "OpenAI-Project": CANARY, "X-Request-Id": CANARY})
    await a.close()
    assert r.status_code == 200 and r.json()["usage"]["input_tokens"] == 0
    assert seen[0].content == raw
    assert seen[0].headers["Authorization"] == "Bearer " + read_key(config.adapter.api_key_file, "cpa_")
    assert seen[0].headers["Accept-Encoding"] == "identity"
    assert not any(k in seen[0].headers for k in ("Cookie", "Forwarded", "OpenAI-Project", "X-Request-Id"))
    assert CANARY not in str(r.headers)
    assert state.auth_state == "ready" and state.active_requests == 0


async def test_models_filter_and_rebuild(config, state):
    c, a = await client(config, state, lambda r: httpx.Response(200, json={"data": [
        {"id": "mock-codex", "credential": CANARY}, {"id": "denied"}]}))
    async with c:
        r = await c.get("/v1/models")
    await a.close()
    assert [x["id"] for x in r.json()["data"]] == ["mock-codex"]
    assert CANARY not in r.text


@pytest.mark.parametrize("status,expected,code", [(401,401,"upstream_reauth_required"), (403,401,"upstream_reauth_required"),
    (429,429,"upstream_rate_limited"), (302,502,"upstream_protocol_error"), (500,502,"upstream_error")])
async def test_errors_no_retry_or_raw_error(config, state, status, expected, code):
    seen = []
    def handler(r):
        seen.append(r)
        return httpx.Response(status, text=CANARY, headers={"Location": "http://evil", "Retry-After": "7"})
    c, a = await client(config, state, handler)
    async with c:
        r = await c.post("/v1/responses", json={"model": "mock-codex"})
    await a.close()
    assert r.status_code == expected and r.json()["error"]["code"] == code
    assert CANARY not in r.text and len(seen) == 1
    assert state.active_requests == 0
    if status == 429:
        assert r.headers["Retry-After"] == "7"


async def test_json_size_compression_models_and_hosted_tools(config, state):
    config.bridge.max_body_bytes = 128
    calls = []
    c, a = await client(config, state, lambda r: calls.append(r))
    async with c:
        assert (await c.post("/v1/responses", content=b"!", headers={"Content-Type": "application/json"})).status_code == 400
        assert (await c.post("/v1/responses", json={"model": "denied"})).status_code == 403
        assert (await c.post("/v1/responses", content=b"x" * 129, headers={"Content-Type": "application/json"})).status_code == 413
        assert (await c.post("/v1/responses", json={}, headers={"Content-Encoding": "gzip"})).status_code == 415
        assert (await c.post("/v1/responses", json={"model": "mock-codex", "stream": "yes"})).status_code == 400
        assert (await c.post("/v1/responses", json={"model": "mock-codex", "tools": [{"type": "web_search"}]})).status_code == 403
    await a.close()
    assert not calls and state.active_requests == 0


async def test_chunked_sse_exact_bytes_and_cleanup(config, state):
    data = (ROOT / "fixtures/responses.complete.sse").read_bytes()
    data = b": comment\r\n\r\n" + data.replace(b"\n", b"\r\n")
    stream = BytesStream([data[i:i + 1] for i in range(len(data))])
    c, a = await client(config, state, lambda r: httpx.Response(200, stream=stream, headers={"Content-Type": "text/event-stream"}))
    async with c:
        r = await c.post("/v1/responses", json={"model": "mock-codex", "stream": True})
    await a.close()
    assert r.content == data and stream.closed and state.active_requests == 0


async def test_concurrency_full_immediate(config, state):
    entered = 0
    gate = asyncio.Event()
    async def handler(r):
        nonlocal entered
        entered += 1
        await gate.wait()
        return httpx.Response(200, json={"status": "completed"})
    c, a = await client(config, state, handler)
    async with c:
        tasks = [asyncio.create_task(c.post("/v1/responses", json={"model": "mock-codex"})) for _ in range(2)]
        for _ in range(100):
            if entered == 2:
                break
            await asyncio.sleep(.001)
        third = await c.post("/v1/responses", json={"model": "mock-codex"})
        assert third.status_code == 429
        gate.set()
        assert all(r.status_code == 200 for r in await asyncio.gather(*tasks))
    await a.close()
    assert state.active_requests == 0


async def test_total_timeout_before_headers(config, state):
    config.bridge.total_seconds = .02
    async def handler(r):
        await asyncio.sleep(1)
        return httpx.Response(200)
    c, a = await client(config, state, handler)
    async with c:
        r = await c.post("/v1/responses", json={"model": "mock-codex"})
    await a.close()
    assert r.status_code == 504 and state.active_requests == 0


async def test_cancel_before_headers_releases_slot(config, state):
    async def handler(r):
        await asyncio.sleep(10)
    c, a = await client(config, state, handler)
    async with c:
        task = asyncio.create_task(c.post("/v1/responses", json={"model": "mock-codex"}))
        await asyncio.sleep(.01)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
    await a.close()
    assert state.active_requests == 0
