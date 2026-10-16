import json

import httpx
import pytest

from windows.cache_diagnostics import CacheDiagnostics
from windows.existing_adapter import ExistingAdapter
from tests.test_single_login import single


def rows(log):
    return json.loads(log.path.read_text(encoding="utf-8"))["requests"]


@pytest.mark.parametrize("stream", [True, False])
async def test_full_input_capture_and_zero_vs_missing_usage(config, state, tmp_path, stream):
    config.adapter.cache_diagnostics_capture_inputs = True
    auth = single(config, tmp_path)
    auth_before = auth.read_bytes()
    usages = [dict(input_tokens=2048, input_tokens_details={"cached_tokens": 0}, output_tokens=1), {}]
    def upstream(request):
        usage = usages.pop(0)
        data = {"type": "response.completed", "response": {"id": "synthetic", "status": "completed",
                                                              "output": [], "usage": usage}}
        raw = ("data: " + json.dumps(data) + "\n\n").encode()
        return httpx.Response(200, stream=httpx.ByteStream(raw), headers={
            "content-type": "text/event-stream", "x-codex-turn-state": "PRIVATE_ROUTE_CANARY"})
    adapter = ExistingAdapter(config, state, transport=httpx.MockTransport(upstream))
    payload = {"model": "mock-codex", "stream": stream, "instructions": "完整系统说明",
               "input": [{"role": "user", "content": [{"type": "input_text", "text": "完整对话正文"}]},
                         {"type": "function_call_output", "call_id": "synthetic-call", "output": "完整工具结果"}],
               "tools": [{"type": "function", "name": "tool", "description": "完整工具定义"}],
               "authorization": "UNTRUSTED_AUTH_CANARY", "access_token": "UNTRUSTED_TOKEN_CANARY"}
    try:
        for _ in range(2):
            r = await adapter.open("POST", "/v1/responses", json.dumps(payload).encode(),
                request_headers=httpx.Headers({"session-id": "PRIVATE_SESSION_CANARY",
                                               "authorization": "UNTRUSTED_HEADER_CANARY"}))
            await r.aread()
            await r.aclose()
        records = rows(adapter.cache_log)
        assert records[0]["cached_input_tokens"] == 0 and records[0]["cache_hit_percent"] == 0
        assert records[0]["usage_status"] == "reported"
        assert records[1]["cached_input_tokens"] is None and records[1]["usage_status"] == "missing"
        assert records[1]["previous_input_is_prefix"] is True
        assert records[0]["stream"] == stream
        captured = json.loads((adapter.cache_log.input_dir / records[0]["request_file"]).read_text(encoding="utf-8"))
        for field in ("input", "tools", "instructions"):
            assert captured[field] == payload[field]
        assert captured["stream"] is True and captured["store"] is False
        for file in [adapter.cache_log.path, *adapter.cache_log.input_dir.iterdir()]:
            text = file.read_text(encoding="utf-8")
            for canary in ("UNTRUSTED_AUTH_CANARY", "UNTRUSTED_TOKEN_CANARY", "UNTRUSTED_HEADER_CANARY",
                           "PRIVATE_ROUTE_CANARY", "PRIVATE_SESSION_CANARY", "SYNTHETIC_REFRESH_NEVER_USE"):
                assert canary not in text
        assert auth.read_bytes() == auth_before
    finally:
        await adapter.close()


async def test_ring_survives_restart_and_old_completion_cannot_restore_evicted_input(config):
    config.adapter.cache_diagnostics_capture_inputs = True
    log = CacheDiagnostics(config)
    headers = {"session-id": "synthetic-session"}
    base = {"model": "mock-codex", "input": [{"role": "user", "content": "one"}], "tools": []}
    first = await log.begin(base, headers)
    first_file = rows(log)[0]["request_file"]
    appended = dict(base, input=base["input"] + [{"role": "assistant", "content": "two"}])
    await log.begin(appended, headers)
    second = rows(log)[-1]
    assert second["previous_input_is_prefix"] is True
    changed = dict(base, tools=[{"type": "function", "name": "changed"}])
    await log.begin(changed, headers)
    assert rows(log)[-1]["input_item_count_decreased"] is True
    assert "tools" in rows(log)[-1]["changed_parameters"]
    for i in range(22):
        await log.begin(dict(base, input=[{"role": "user", "content": str(i)}]), headers)
    assert len(rows(log)) == 20
    assert len(list(log.input_dir.glob("*.request.json"))) == 20
    assert not (log.input_dir / first_file).exists()
    await log.update(first, transport_outcome="eof")
    assert len(rows(log)) == 20 and all(r["id"] != first for r in rows(log))
    restored = CacheDiagnostics(config)
    assert len(rows(restored)) == 20
    assert all(r["transport_outcome"] == "interrupted_by_restart" for r in rows(restored))
    assert restored.key == log.key


async def test_log_write_failure_does_not_change_model_response(config, state, tmp_path, monkeypatch):
    single(config, tmp_path)
    raw = b'data: {"type":"response.completed","response":{"id":"synthetic","status":"completed","output":[]}}\n\n'
    adapter = ExistingAdapter(config, state, transport=httpx.MockTransport(lambda r:
        httpx.Response(200, stream=httpx.ByteStream(raw), headers={"content-type": "text/event-stream"})))
    def fail():
        raise OSError("SYNTHETIC_IO_ERROR_NEVER_LOG")
    monkeypatch.setattr(adapter.cache_log, "_save", fail)
    try:
        r = await adapter.open("POST", "/v1/responses", b'{"model":"mock-codex","stream":true,"input":"synthetic"}')
        assert await r.aread() == raw
        await r.aclose()
        assert adapter.cache_log.write_error
    finally:
        await adapter.close()


async def test_truncated_stream_is_not_logged_as_cache_zero(config, state, tmp_path):
    single(config, tmp_path)
    raw = b'data: {"type":"response.completed","response":'
    adapter = ExistingAdapter(config, state, transport=httpx.MockTransport(lambda r:
        httpx.Response(200, stream=httpx.ByteStream(raw), headers={"content-type": "text/event-stream"})))
    try:
        r = await adapter.open("POST", "/v1/responses", b'{"model":"mock-codex","stream":true}')
        with pytest.raises(ValueError,match='missing_terminal'):
            await r.aread()
        await r.aclose()
        record = rows(adapter.cache_log)[0]
        assert record["cached_input_tokens"] is None and record["usage_status"] == "incomplete"
    finally:
        await adapter.close()


async def test_plaintext_capture_requires_explicit_opt_in(config):
    assert config.adapter.cache_diagnostics_capture_inputs is False
    log = CacheDiagnostics(config)
    await log.begin({"model": "mock-codex", "input": "PRIVATE_INPUT_NOT_CAPTURED"})
    assert rows(log)[0]["request_file"] is None
    assert not list(log.input_dir.iterdir())
    assert "PRIVATE_INPUT_NOT_CAPTURED" not in log.path.read_text(encoding="utf-8")
