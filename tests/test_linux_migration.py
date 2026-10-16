import asyncio
import base64
import hashlib
import json
import time
from types import SimpleNamespace
import pytest
from linux_worker.auth import ManagedAuth
from linux_worker.config import LocalTelemetry
from pydantic import ValidationError
from test_server_regression import service


def auth_file(path, expiry):
    claims = base64.urlsafe_b64encode(json.dumps({"sub": "synthetic", "exp": expiry}).encode()).decode().rstrip("=")
    path.write_text(json.dumps({"tokens": {"access_token": "test."+claims+".test", "account_id": "synthetic-workspace"}}))


class Log:
    def __init__(self): self.events = []
    def emit(self, event, **fields): self.events.append(event)
    def observe_auth(self, path): pass


async def test_managed_refresh_coalesces_sessions(tmp_path):
    path = tmp_path / "auth.json"
    auth_file(path, time.time()-1)
    binary = tmp_path / "codex"
    binary.write_bytes(b"synthetic")
    calls = []
    class Client:
        def __init__(self, *args, **kwargs):
            assert kwargs["external_access"] is False
            self.process = SimpleNamespace(returncode=None)
        async def start(self): pass
        async def close(self): pass
        async def request(self, method, params):
            calls.append((method, params))
            await asyncio.sleep(.01)
            auth_file(path, time.time()+3600)
            return {"account": {"type": "chatgpt"}}
    config = SimpleNamespace(adapter=SimpleNamespace(existing_auth_file=path, outbound_proxy="http://127.0.0.1:17890"),
        quota=SimpleNamespace(executable=binary, sha256=hashlib.sha256(binary.read_bytes()).hexdigest(),
            codex_home=tmp_path, request_timeout_seconds=15))
    log = Log()
    manager = ManagedAuth(config, log, Client)
    await asyncio.gather(*(manager.ensure() for _ in range(3)))
    assert calls == [("account/read", {"refreshToken": True})]
    assert log.events == ["credential_refresh_started", "credential_refresh_finished"]
    await manager.close()


async def test_failed_refresh_is_throttled_and_expired_auth_stays_rejected(tmp_path):
    path = tmp_path / "auth.json"
    auth_file(path, time.time()-1)
    binary = tmp_path / "codex"; binary.write_bytes(b"synthetic")
    calls = []
    class Client:
        def __init__(self, *args, **kwargs): self.process = SimpleNamespace(returncode=None)
        async def start(self): pass
        async def close(self): pass
        async def request(self, *args):
            calls.append(True)
            raise TimeoutError()
    config = SimpleNamespace(adapter=SimpleNamespace(existing_auth_file=path, outbound_proxy=None),
        quota=SimpleNamespace(executable=binary, sha256=hashlib.sha256(binary.read_bytes()).hexdigest(),
            codex_home=tmp_path, request_timeout_seconds=15))
    manager = ManagedAuth(config, Log(), Client)
    with pytest.raises(TimeoutError): await manager.ensure()
    with pytest.raises(ValueError): await manager.ensure()
    assert len(calls) == 1
    await manager.close()


def test_telemetry_cannot_send_plaintext_credentials_off_host(tmp_path):
    LocalTelemetry(url="http://127.0.0.1:8083/internal/v1/telemetry", secret_file=tmp_path/"tel.key")
    for url in ("http://example.com/internal/v1/telemetry", "http://127.0.0.1:8080/internal/v1/telemetry", "https://example.com/internal/v1/telemetry"):
        with pytest.raises(ValidationError): LocalTelemetry(url=url, secret_file=tmp_path/"tel.key")


async def test_rolling_gateway_does_not_recover_live_wallet_or_reset_worker(service, tmp_path, monkeypatch):
    from codex_relay import api_proxy, worker_state
    from codex_relay.app import create_app
    from test_key_wallet import key_request
    _, key, request_id, _, _ = key_request(service, balance="1")
    service.db.execute("UPDATE workers SET state='online',last_seen_at='2099-01-01T00:00:00Z'")
    probes = []
    async def probe(_): probes.append(True)
    monkeypatch.setattr(api_proxy, "probe_worker", probe)
    activation = tmp_path / "activate"
    app = create_app(service.config, services=service, rolling_activation_file=activation)
    async with app.router.lifespan_context(app):
        await asyncio.sleep(.15)
        assert service.db.query_one("SELECT ended_at FROM requests WHERE request_id=?", (request_id,))[0] is None
        assert service.db.query_one("SELECT balance_blocked FROM client_keys WHERE key_id=?", (key.key_id,))[0] == 0
        assert not probes
        activation.touch()
        await asyncio.sleep(.15)
        assert probes
