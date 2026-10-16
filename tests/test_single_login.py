import base64
import json
from pathlib import Path

import httpx
import pytest

from windows.existing_auth import ExistingAuth
from windows.existing_adapter import ExistingAdapter, BASE
from windows.identity import Identity
from windows.quota_client import QuotaClient


def session(path, sub="synthetic-user", account="synthetic-workspace", suffix="one"):
    claims = base64.urlsafe_b64encode(json.dumps({"sub": sub}).encode()).decode().rstrip("=")
    path.write_text(json.dumps({"auth_mode": "chatgpt", "tokens": {"access_token": "synthetic." + claims + "." + suffix,
        "account_id": account, "refresh_token": "SYNTHETIC_REFRESH_NEVER_USE"}}))


def single(config, tmp_path):
    path = tmp_path / "current" / "auth.json"
    path.parent.mkdir()
    session(path)
    config.enable_mock = False
    config.adapter.mode, config.adapter.existing_auth_file = "existing_codex_login", path
    return path


def test_existing_access_never_copies_refresh(config, tmp_path):
    path = single(config, tmp_path)
    before = path.read_bytes()
    auth = ExistingAuth(path)
    assert "SYNTHETIC_REFRESH" not in json.dumps(auth.external_params())
    assert "SYNTHETIC_REFRESH" not in json.dumps(auth.headers())
    assert path.read_bytes() == before


def test_identity_survives_token_rotation_but_changes_workspace(config, tmp_path):
    path = single(config, tmp_path)
    identity = Identity(config)
    epoch = identity.epoch
    assert identity.check == "automatic_match"
    session(path, suffix="two")
    identity.reload()
    assert identity.epoch == epoch
    session(path, account="different-workspace")
    identity.reload()
    assert identity.epoch != epoch


async def test_catalog_uses_configured_version_and_exposes_new_model(config, state, tmp_path):
    single(config, tmp_path)
    config.adapter.catalog_client_version = "0.160.0"
    def handler(request):
        assert request.url.params["client_version"] == "0.160.0"
        return httpx.Response(200, json={"models": [{"slug": "gpt-6.1-sol", "supported_in_api": True}]})
    adapter = ExistingAdapter(config, state, transport=httpx.MockTransport(handler))
    try:
        assert (await adapter.catalog()).json()["data"] == [{"id": "gpt-6.1-sol"}]
    finally:
        await adapter.close()


async def test_external_bootstrap_only_enabled_locally(tmp_path):
    client = QuotaClient([], tmp_path)
    with pytest.raises(ValueError):
        await client.use_existing_access({"type": "chatgpt"})
    with pytest.raises(ValueError):
        await client.request("account/login/start", {})


def test_quota_uses_explicit_proxy_and_ignores_inherited_proxy(tmp_path, monkeypatch):
    from windows.quota_client import quota_environment
    monkeypatch.setenv("ALL_PROXY", "socks5://unexpected.example:1")
    monkeypatch.setenv("HTTPS_PROXY", "http://unexpected.example:1")
    env = quota_environment(tmp_path, "http://127.0.0.1:7890", True)
    assert env["HTTPS_PROXY"] == "http://127.0.0.1:7890" and "ALL_PROXY" not in env


@pytest.mark.parametrize("stream", [True, False])
async def test_single_adapter_preserves_tools_and_does_not_refresh(config, state, tmp_path, stream):
    path = single(config, tmp_path)
    before = path.read_bytes()
    response = {"id": "resp_synthetic", "status": "completed", "model": "mock-codex", "output": [], "usage": {"input_tokens": 1, "output_tokens": 1, "total_tokens": 2}}
    sse = ("event: response.completed\ndata: " + json.dumps({"type": "response.completed", "response": response}) + "\n\n").encode()
    requests = []
    def handler(r):
        requests.append(r)
        assert str(r.url) == BASE + "/responses"
        assert "SYNTHETIC_REFRESH" not in str(r.headers)
        return httpx.Response(200, stream=httpx.ByteStream(sse), headers={"content-type": "text/event-stream"})
    adapter = ExistingAdapter(config, state, transport=httpx.MockTransport(handler))
    payload = {"model": "mock-codex", "stream": stream, "input": [{"type": "function_call_output", "call_id": "call_a", "output": "synthetic-result"}],
               "tools": [{"type": "function", "name": "test", "parameters": {"type": "object"}}], "reasoning": {"effort": "low"}, "future_field": {"x": 1}}
    try:
        r = await adapter.open("POST", "/v1/responses", json.dumps(payload).encode())
        assert (await r.aread()) == sse if stream else r.json() == response
        sent = json.loads(requests[0].content)
        assert sent["input"] == payload["input"] and sent["tools"] == payload["tools"]
        assert sent["reasoning"] == payload["reasoning"] and sent["future_field"] == payload["future_field"]
        assert len(requests) == 1 and path.read_bytes() == before
    finally:
        await adapter.close()


async def test_401_never_refreshes_or_retries(config, state, tmp_path):
    path = single(config, tmp_path)
    requests = []
    def handler(r):
        requests.append(r)
        return httpx.Response(401)
    a = ExistingAdapter(config, state, transport=httpx.MockTransport(handler))
    try:
        r = await a.open("POST", "/v1/responses", b'{"model":"mock-codex","stream":true}')
        assert r.status_code == 401 and len(requests) == 1
        assert len(list(path.parent.iterdir())) == 1
    finally:
        await a.close()


async def test_missing_backend_header_and_sparse_terminal_output(config, state, tmp_path):
    single(config, tmp_path)
    item = {"type": "function_call", "call_id": "synthetic-call", "name": "test", "arguments": "{}"}
    events = [{"type": "response.output_item.done", "output_index": 0, "item": item},
              {"type": "response.completed", "response": {"id": "synthetic", "status": "completed", "output": []}}]
    raw = "".join("data: " + json.dumps(o) + "\n\n" for o in events).encode()
    a = ExistingAdapter(config, state, transport=httpx.MockTransport(lambda r: httpx.Response(200, stream=httpx.ByteStream(raw))))
    try:
        r = await a.open("POST", "/v1/responses", b'{"model":"mock-codex","stream":false}')
        assert r.json()["output"] == [item]
    finally:
        await a.close()


async def test_switch_during_quota_query_cannot_attribute_data_to_old_identity(config, state, tmp_path):
    from windows.quota_service import QuotaService
    from types import SimpleNamespace
    path = single(config, tmp_path)
    config.quota.executable = tmp_path / "synthetic-codex.exe"
    service = QuotaService(config, state)
    old_epoch = service.identity.epoch
    class Client:
        process = SimpleNamespace(returncode=None)
        async def use_existing_access(self, params): pass
        async def close(self): pass
        async def request(self, method, params=None):
            if method == "account/read":
                return {"account": {"type": "chatgpt"}}
            session(path, account="different-workspace")
            return {"rateLimits": {"primary": {"usedPercent": 10, "windowDurationMins": 300}}}
    service.client = Client()
    assert not await service.refresh()
    assert state.quota["windows"] == [] and state.quota["captured_at"] is None
    assert service.identity.epoch != old_epoch
