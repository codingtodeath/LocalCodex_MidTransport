import asyncio
import base64
import json
import ssl
import time
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import httpx
import pytest

from windows.existing_adapter import ExistingAdapter
from windows.runtime_diagnostics import RuntimeDiagnostics, exception_info
from windows.telemetry import Commands, Telemetry
from windows.processes import ManagedProcess
from tests.test_single_login import single


def rows(diag):
    return [json.loads(line) for line in diag.path.read_text().splitlines()]


def test_tls_reason_is_allowlisted_without_exception_message(config):
    exc = ssl.SSLError(1, "SECRET_EXCEPTION_TEXT")
    exc.reason = "WRONG_VERSION_NUMBER"
    assert exception_info(exc)["tls_reason"] == "WRONG_VERSION_NUMBER"
    diag = RuntimeDiagnostics(config)
    diag.emit("inference_open_failed", **exception_info(exc))
    exc.reason = "SECRET_HOST_OR_TOKEN"
    assert "tls_reason" not in exception_info(exc)
    diag.emit("inference_open_failed", tls_reason=exc.reason, **exception_info(exc))
    text = diag.path.read_text()
    assert "WRONG_VERSION_NUMBER" in text
    assert "SECRET" not in text


def write_auth(path, *, hours=3, suffix="SECRET_ACCESS", account="SECRET_ACCOUNT", refresh="2026-10-01T00:00:00Z"):
    claims = {"sub": "SECRET_USER", "exp": int((datetime.now(timezone.utc)+timedelta(hours=hours)).timestamp())}
    segment = base64.urlsafe_b64encode(json.dumps(claims).encode()).decode().rstrip("=")
    path.write_text(json.dumps({"tokens": {"access_token": "JWT."+segment+"."+suffix,
        "account_id": account, "refresh_token": "SECRET_REFRESH"}, "last_refresh": refresh}))


def test_credentials_rotation_expiry_and_recovery_are_safe(config, tmp_path):
    auth = single(config, tmp_path)
    write_auth(auth)
    original = auth.read_bytes()
    diag = RuntimeDiagnostics(config)
    diag.observe_auth(auth); diag.observe_auth(auth)
    assert [r["event"] for r in rows(diag)] == ["credential_observed", "credential_valid"]
    assert auth.read_bytes() == original
    write_auth(auth, suffix="ROTATED_SECRET", refresh="2026-10-05T01:00:00Z")
    diag.observe_auth(auth)
    rotation = rows(diag)[-1]
    assert rotation["event"] == "credential_changed" and rotation["access_token_changed"]
    assert rotation["refresh_metadata_changed"] and not rotation["identity_changed"]
    write_auth(auth, hours=-1, account="OTHER_SECRET_ACCOUNT")
    diag.observe_auth(auth)
    assert rows(diag)[-1]["event"] == "credential_expired"
    assert rows(diag)[-2]["identity_changed"]
    auth.write_text('{"tokens": "SECRET_BAD_PAYLOAD"}')
    diag.observe_auth(auth)
    assert rows(diag)[-1]["event"] == "credentials_failed"
    write_auth(auth); diag.observe_auth(auth)
    assert rows(diag)[-1]["event"] == "credentials_recovered"
    text = diag.path.read_text()
    for secret in ("SECRET", "JWT.", "Authorization", "access_token\"", "refresh_token", "account_id", "fingerprint"):
        assert secret not in text


def test_rotation_is_bounded_and_schema_rejects_secret_fields(config):
    diag = RuntimeDiagnostics(config, max_bytes=400, backups=2)
    for _ in range(30):
        diag.emit("openai_check_failed", http_status=401, error_code="unauthorized", error_type={"secret": "AUTH"},
            authorization="SECRET_BEARER", body="SECRET_BODY", url="SECRET_URL")
    files = list(diag.path.parent.glob("runtime-events*.jsonl"))
    assert len(files) == 3 and all(p.stat().st_size <= 400 for p in files)
    for p in files:
        assert "SECRET" not in p.read_text()
        for line in p.read_text().splitlines(): assert json.loads(line)["http_status"] == 401


def test_write_failure_is_nonfatal_and_status_recovers(config, monkeypatch):
    diag = RuntimeDiagnostics(config)
    with monkeypatch.context() as m:
        m.setattr("windows.runtime_diagnostics.os.open", lambda *a, **k: (_ for _ in ()).throw(PermissionError("SECRET")))
        diag.emit("worker_started")
        assert diag.write_error
    diag.emit("worker_started")
    assert not diag.write_error


async def test_real_request_distinguishes_timeout_auth_rejection_and_recovery(config, state, tmp_path):
    auth = single(config, tmp_path);write_auth(auth)
    mode = "timeout"
    async def handler(request):
        if mode == "timeout": raise httpx.ReadTimeout('SECRET_URL', request=request)
        if mode == "unauthorized": return httpx.Response(401, text="SECRET_RESPONSE")
        return httpx.Response(200, stream=httpx.ByteStream(b'data: {}\n\n'))
    adapter = ExistingAdapter(config, state, transport=httpx.MockTransport(handler))
    try:
        with pytest.raises(httpx.ReadTimeout): await adapter.open('POST', '/v1/responses', b'{"model":"mock-codex","stream":true}')
        mode = "unauthorized"; r = await adapter.open('POST', '/v1/responses', b'{"model":"mock-codex","stream":true}')
        assert r.status_code == 401; await r.aclose()
        mode = "ok"; r = await adapter.open('POST', '/v1/responses', b'{"model":"mock-codex","stream":true}')
        assert r.status_code == 200; await r.aclose()
        events = rows(adapter.diagnostics)
        failed = [r for r in events if r["event"] == "inference_open_failed"]
        assert failed[0]["error_code"] == "read_timeout"
        rejected = [r for r in events if r['event'] == 'inference_http_error'][0]
        assert rejected['error_code'] == 'unauthorized' and rejected['http_status'] == 401
        assert state.auth_state == 'ready'
        assert "SECRET" not in adapter.diagnostics.path.read_text()
        assert adapter.diagnostics is state.diagnostics
    finally: await adapter.close()


async def test_local_readiness_and_credential_rotation_are_logged(config, state, tmp_path):
    auth = single(config, tmp_path);write_auth(auth)
    adapter = ExistingAdapter(config, state, transport=httpx.MockTransport(
        lambda _: httpx.Response(200, json={"models": [{"slug": "mock-codex"}]})))
    try:
        assert await adapter.probe()
        write_auth(auth, suffix="ROTATED_SECRET")
        assert await adapter.probe()
        assert any(r['event'] == 'credential_changed' for r in rows(adapter.diagnostics))
        write_auth(auth, hours=-1)
        assert not await adapter.probe()
        assert rows(adapter.diagnostics)[-1]["error_code"] == "auth_unavailable"
    finally: await adapter.close()


async def test_telemetry_failure_and_recovery_without_body_or_key(config, state):
    config.telemetry.url = "https://relay.invalid/internal/v1/telemetry"
    commands = Commands(config.runtime_dir / "commands.sqlite")
    status = 503
    def handler(request):
        if status != 200: return httpx.Response(status, text="SECRET_RESPONSE")
        body = json.loads(request.content)
        return httpx.Response(200, json={"protocol_version": "1.0", "ack_seq": body["seq"],
            "server_time": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"), "commands": []})
    t = Telemetry(config, state, commands, transport=httpx.MockTransport(handler))
    try:
        assert not await t.send()
        status = 200; assert await t.send()
        events = rows(t.diagnostics)
        assert events[-2]["event"] == "telemetry_failed" and events[-2]["http_status"] == 503
        assert events[-1]["event"] == "telemetry_recovered"
        assert "SECRET" not in t.diagnostics.path.read_text()
        assert config.telemetry.secret_file.read_text() not in t.diagnostics.path.read_text()
    finally:
        await t.close();commands.close()


async def test_tunnel_exit_retry_start_stop_records_no_command(config, monkeypatch):
    diag = RuntimeDiagnostics(config)
    class Process:
        pid = 123; returncode = None
        def terminate(self): self.returncode = 0
        async def wait(self): return self.returncode
    async def spawn(*args, **kwargs): return Process()
    monkeypatch.setattr("windows.processes.asyncio.create_subprocess_exec", spawn)
    p = ManagedProcess(["ssh", "SECRET_KEY_PATH"], config.runtime_dir,
        SimpleNamespace(assign=lambda _: None), diagnostics=diag)
    await p.tick();p.process.returncode = 255;await p.tick()
    p.next_start = 0;await p.tick();await p.stop()
    events = rows(diag)
    assert [r["event"] for r in events] == ["tunnel_started", "tunnel_exited", "tunnel_started", "tunnel_stopped"]
    assert events[1]["exit_code"] == 255 and events[1]["retry_seconds"] > 0
    assert "SECRET" not in diag.path.read_text()


async def test_inference_network_exception_recorded_and_propagated(config, state, tmp_path):
    single(config, tmp_path)
    def handler(request): raise httpx.ConnectError("SECRET_REQUEST_URL", request=request)
    adapter = ExistingAdapter(config, state, transport=httpx.MockTransport(handler))
    try:
        with pytest.raises(httpx.ConnectError):
            await adapter.open("POST", "/v1/responses", json.dumps({"model":"mock-codex","input":"test"}))
        events = rows(adapter.diagnostics)
        assert events[-1]["event"] == "inference_open_failed"
        assert events[-1]["error_code"] == "connect_error"
        assert "SECRET" not in adapter.diagnostics.path.read_text()
    finally: await adapter.close()


async def test_stream_failure_is_logged_without_changing_cancellation(config):
    from windows.cache_diagnostics import DiagnosticStream
    diag = RuntimeDiagnostics(config)
    class Stream(httpx.AsyncByteStream):
        async def __aiter__(self):
            yield b"data: {}\n\n"
            raise httpx.ReadError("SECRET_RESPONSE_BODY")
    async def update(*args, **kwargs): pass
    stream = DiagnosticStream(Stream(), SimpleNamespace(update=update), "id", 200, time.monotonic(), diag)
    with pytest.raises(httpx.ReadError):
        async for _ in stream: pass
    assert any(r["event"] == "inference_stream_failed" for r in rows(diag))
    assert rows(diag)[-1]["event"] == "inference_stream_finished"
    assert rows(diag)[-1]["bytes_received"] > 0 and not rows(diag)[-1]["terminal_seen"]
    assert "SECRET" not in diag.path.read_text()


def test_exception_tls_cause_classification_never_uses_message():
    import ssl
    exc = httpx.ConnectError("SECRET_ENDPOINT")
    exc.__cause__ = ssl.SSLCertVerificationError("SECRET_CERT")
    assert exception_info(exc) == {"error_code":"tls_error", "error_type":"SSLError"}


def test_timeout_during_tls_read_is_not_misreported_as_certificate_error():
    import ssl
    pending = ssl.SSLWantReadError("SECRET_TLS_CONTEXT")
    cancelled = asyncio.CancelledError();cancelled.__context__ = pending
    exc = TimeoutError();exc.__cause__ = cancelled
    assert exception_info(exc) == {"error_code":"timeout", "error_type":"TimeoutError"}
    assert exception_info(cancelled) == {"error_code":"cancelled", "error_type":"CancelledError"}
