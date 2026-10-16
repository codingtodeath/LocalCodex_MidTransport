"""Synthetic checks for Codex routing continuity; never call a live model."""
import json

import httpx
import pytest

from codex_relay.api_proxy import build_downstream_headers
from windows.bridge import create_app
from windows.config import read_key
from windows.existing_adapter import ExistingAdapter
from windows.routing import routing_headers
from tests.test_single_login import single


@pytest.mark.parametrize("stream", [True, False])
async def test_routing_round_trip_does_not_cross_turns(config, state, tmp_path, stream):
    auth_path = single(config, tmp_path)
    before = auth_path.read_bytes()
    response = {"id": "synthetic", "status": "completed", "output": [],
                "usage": {"input_tokens": 2048, "input_tokens_details": {"cached_tokens": 1024},
                          "output_tokens": 1, "total_tokens": 2049}}
    raw = ("data: " + json.dumps({"type": "response.completed", "response": response}) + "\n\n").encode()
    seen = []

    def upstream(request):
        seen.append(request)
        return httpx.Response(200, stream=httpx.ByteStream(raw), headers={
            "content-type": "text/event-stream", "x-codex-turn-state": "synthetic-route",
            "set-cookie": "SYNTHETIC_SECRET", "authorization": "SYNTHETIC_SECRET"})

    adapter = ExistingAdapter(config, state, transport=httpx.MockTransport(upstream))
    client = httpx.AsyncClient(transport=httpx.ASGITransport(app=create_app(config, state, adapter)),
                              base_url="http://synthetic-bridge", headers={
        "authorization": "Bearer " + read_key(config.bridge.auth_secret_file, "wkr_")})
    payload = {"model": "mock-codex", "stream": stream, "instructions": "stable instructions",
               "prompt_cache_key": "synthetic-conversation", "prompt_cache_options": {"mode": "explicit"},
               "input": [{"role": "user", "content": [{"type": "input_text", "text": "stable prefix",
                          "prompt_cache_breakpoint": {"mode": "explicit"}}]}],
               "tools": [{"type": "function", "name": "synthetic", "parameters": {"type": "object"}}],
               "reasoning": {"effort": "medium"}, "include": ["reasoning.encrypted_content"]}
    try:
        async with client:
            for turn_state in (None, "synthetic-route", None):
                headers = {"session-id": "synthetic-session", "thread-id": "synthetic-thread",
                           "chatgpt-account-id": "UNTRUSTED_ACCOUNT", "cookie": "UNTRUSTED_COOKIE"}
                if turn_state:
                    headers["x-codex-turn-state"] = turn_state
                r = await client.post("/v1/responses", json=payload, headers=headers)
                assert r.status_code == 200
                assert r.headers.get("x-codex-turn-state") == "synthetic-route"
                assert "SYNTHETIC_SECRET" not in str(r.headers)
                assert r.content == raw if stream else r.json() == response
                sent = seen[-1]
                assert sent.headers["session-id"] == "synthetic-session"
                assert sent.headers["thread-id"] == "synthetic-thread"
                assert sent.headers.get("x-codex-turn-state") == turn_state
                assert sent.headers["chatgpt-account-id"] == "synthetic-workspace"
                assert "UNTRUSTED_COOKIE" not in sent.headers.get("cookie", "")
                assert json.loads(sent.content) == dict(payload, stream=True, store=False)
        assert auth_path.read_bytes() == before and state.active_requests == 0
    finally:
        await adapter.close()


def test_gateway_preserves_only_safe_routing_response_headers():
    headers = build_downstream_headers(httpx.Response(200, headers={
        "x-codex-turn-state": "synthetic-route", "set-cookie": "SYNTHETIC_SECRET",
        "authorization": "SYNTHETIC_SECRET", "content-type": "text/event-stream"}))
    assert headers.get("x-codex-turn-state") == "synthetic-route"
    assert "SYNTHETIC_SECRET" not in str(headers)


def test_routing_headers_are_bounded_and_unambiguous():
    headers = httpx.Headers([("session-id", "first"), ("session-id", "second"),
                             ("thread-id", "a" * 4097), ("x-codex-turn-state", "bad\nvalue"),
                             ("authorization", "SYNTHETIC_SECRET")])
    assert routing_headers(headers) == {}
    assert routing_headers({"session-id": "synthetic", "x-codex-turn-state": "route"}, response=True) == {
        "x-codex-turn-state": "route"}
