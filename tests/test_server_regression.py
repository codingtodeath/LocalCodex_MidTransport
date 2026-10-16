import asyncio
import copy
import json
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace

import httpx
import pytest

from codex_relay import auth, worker_state, commands, api_proxy
from codex_relay.app import create_app
from codex_relay.config import RelayConfig
from codex_relay.services import build_services

ROOT = Path(__file__).resolve().parents[1]


@pytest.fixture
def service(tmp_path):
    config = RelayConfig(db_path=str(tmp_path / "server.db"), models_allowlist=("mock-codex",), probe_interval_seconds=1)
    services = build_services(config, pepper_bytes=b"synthetic_pepper_12345678901234567890",
                              session_secret=b"synthetic_session_12345678901234567890", worker_key="wkr_synthetic")
    worker_state.ensure_worker(services.db, config.worker_id)
    auth.upsert_worker_credential(services.db, config.worker_id, auth.ROLE_TELEMETRY, "tel_synthetic", services.pepper)
    yield services
    services.db.close()


def heartbeat(service, instance, now, seq=1):
    worker_state.record_heartbeat(service.db, worker_id=service.config.worker_id, instance_id=instance, seq=seq,
        sent_at=now.isoformat(), capabilities={}, local={"adapter_ready": True, "bridge_ready": True}, now=now)


async def test_monitor_assets_revalidate_and_private_data_requires_login(service):
    app = create_app(service.config, services=service)
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
        page = await client.get("/")
        assert page.status_code == 200 and 'id="boot-status"' in page.text
        asset = await client.get("/assets/app.js?v=20261004-2")
        assert asset.status_code == 200 and asset.headers["Cache-Control"] == "no-cache"
        assert (await client.get("/assets/missing.js")).status_code == 404
        assert (await client.get("/admin/api/overview")).status_code == 401


def test_stale_probe_cannot_make_new_heartbeat_online(service):
    now = datetime.now(timezone.utc)
    instance = str(uuid.uuid4())
    heartbeat(service, instance, now)
    worker_state.record_probe(service.db, service.config.worker_id, ok=True, instance_id=instance, now=now-timedelta(seconds=20))
    snapshot = worker_state.evaluate(service.db, service.config.worker_id, service.config, now=now)
    assert snapshot.state == "degraded"
    assert not worker_state.is_servable(snapshot)[0]


def test_restart_discards_probe_even_if_state_column_never_written_online(service):
    now = datetime.now(timezone.utc)
    instance = str(uuid.uuid4())
    heartbeat(service, instance, now)
    worker_state.record_probe(service.db, service.config.worker_id, ok=True, instance_id=instance, now=now)
    assert worker_state.evaluate(service.db, service.config.worker_id, service.config).state == "online"
    worker_state.reset_after_restart(service.db)
    snapshot = worker_state.evaluate(service.db, service.config.worker_id, service.config)
    assert snapshot.state == "unknown" and snapshot.probe_ok is None and not snapshot.heartbeat_fresh


async def test_probe_timer_runs_without_http_requests(service, monkeypatch):
    calls = []
    async def probe(services):
        calls.append(True)
    monkeypatch.setattr(api_proxy, "probe_worker", probe)
    app = create_app(service.config, services=service)
    async with app.router.lifespan_context(app):
        await asyncio.sleep(1.1)
        assert len(calls) >= 2
    count = len(calls)
    await asyncio.sleep(.05)
    assert len(calls) == count


@pytest.mark.parametrize("mutate", [
    lambda d: d.update(seq="1"),
    lambda d: d["local"].update(bridge_ready="true"),
    lambda d: d["local"].pop("last_inference_success_at"),
    lambda d: d.update(sent_at="2026-10-04T12:00:00"),
    lambda d: d["quota"]["windows"][0].update(used_percent=float("nan")),
])
async def test_schema_rejects_coercions_and_missing_required(service, mutate):
    data = json.loads((ROOT / "fixtures/telemetry.ok.json").read_text(encoding="utf-8"))
    mutate(data)
    app = create_app(service.config, services=service)
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app), base_url="http://server") as c:
        r = await c.post("/internal/v1/telemetry", content=json.dumps(data).encode(),
                         headers={"Authorization": "Bearer tel_synthetic", "Content-Type": "application/json"})
    assert r.status_code == 400


async def test_rejected_unknown_field_never_persists_canary(service):
    data = json.loads((ROOT / "fixtures/telemetry.ok.json").read_text(encoding="utf-8"))
    data["SYNTHETIC_CANARY_UNTRUSTED_FIELD"] = "SYNTHETIC_CANARY_VALUE"
    app = create_app(service.config, services=service)
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app), base_url="http://server") as c:
        r = await c.post("/internal/v1/telemetry", json=data, headers={"Authorization": "Bearer tel_synthetic"})
    assert r.status_code == 400 and "SYNTHETIC_CANARY" not in r.text
    rows = service.db.query("SELECT detail_json FROM audit_events")
    assert "SYNTHETIC_CANARY" not in str([dict(row) for row in rows])


async def test_bounded_chunked_body_stops_reading_after_limit():
    consumed = []
    class Request:
        headers = {}
        async def body(self):
            raise AssertionError("unbounded body read used")
        async def stream(self):
            for i in range(100):
                consumed.append(i)
                yield b"x" * 10
    from codex_relay.errors import RelayError
    with pytest.raises(RelayError) as exc:
        await api_proxy.read_bounded_body(Request(), 25)
    assert exc.value.status_code == 413 and len(consumed) == 3


def test_command_cannot_regress_from_done_to_accepted(service):
    result = commands.create_refresh_command(service.db, service.config.worker_id, service.config, worker_servable=True)
    ident = result.command_id
    commands.take_pending_command(service.db, service.config.worker_id)
    assert commands.record_command_result(service.db, service.config.worker_id, ident, "done")
    assert not commands.record_command_result(service.db, service.config.worker_id, ident, "accepted")
    assert commands.command_status(service.db, ident)["status"] == "done"


async def test_probe_pool_ignores_environment_proxy(service, monkeypatch):
    monkeypatch.setenv("HTTP_PROXY", "http://127.0.0.1:1")
    client = api_proxy.make_probe_client(service)
    try:
        assert not client._trust_env
    finally:
        await client.aclose()


async def stream_call(service, monkeypatch, delay):
    import time
    from dataclasses import replace
    service.config = replace(service.config, request_deadline_seconds=.08)
    token, key = auth.create_client_key(service.db, service.pepper, "synthetic-client")
    class Slow(httpx.AsyncByteStream):
        closed = False
        async def __aiter__(self):
            yield b'event: response.created\ndata: {"type":"response.created"}\n\n'
            await asyncio.sleep(delay)
        async def aclose(self):
            self.closed = True
    source = Slow()
    def client(services):
        return httpx.AsyncClient(transport=httpx.MockTransport(lambda r: httpx.Response(200, stream=source)), trust_env=False)
    monkeypatch.setattr(api_proxy, "make_client", client)
    request_id, attempt_id = str(uuid.uuid4()), str(uuid.uuid4())
    started = datetime.now(timezone.utc).isoformat()
    service.gate.try_acquire(key.key_id)
    api_proxy._create_request_rows(service, request_id=request_id, attempt_id=attempt_id, client_key_id=key.key_id,
        client_alias=key.alias, requested_model="mock-codex", stream=True, started_at=started)
    response = await api_proxy._handle_stream(service, url="http://synthetic/v1/responses", headers={}, body=b'{}',
        request_id=request_id, attempt_id=attempt_id, client_key=key, requested_model="mock-codex",
        started_at=started, started_monotonic=time.monotonic())
    return response, source, request_id


async def test_stream_total_deadline_interrupts_silent_upstream(service, monkeypatch):
    response, source, ident = await stream_call(service, monkeypatch, 10)
    with pytest.raises(TimeoutError):
        async for _ in response.body_iterator:
            pass
    assert source.closed and service.gate.global_active == 0
    row = service.db.query_one("SELECT response_status, error_code FROM requests WHERE request_id=?", (ident,))
    assert row["response_status"] == "interrupted" and row["error_code"] == "request_deadline_exceeded"


async def test_stream_disconnect_releases_slot_and_persists(service, monkeypatch):
    response, source, ident = await stream_call(service, monkeypatch, 10)
    received = asyncio.Event()
    async def consume():
        async for _ in response.body_iterator:
            received.set()
    task = asyncio.create_task(consume())
    await received.wait()
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert source.closed and service.gate.global_active == 0
    row = service.db.query_one("SELECT response_status FROM requests WHERE request_id=?", (ident,))
    assert row["response_status"] == "interrupted"


async def test_unstarted_stream_background_releases_slot(service, monkeypatch):
    response, source, ident = await stream_call(service, monkeypatch, 10)
    await response.background()
    await response.background()
    assert source.closed and service.gate.global_active == 0


def test_conflicting_terminal_and_truncated_terminal_are_not_billed():
    from codex_relay.stream_meter import meter_sse_text
    first = {"type":"response.completed", "response":{"id":"resp_synthetic", "status":"completed", "usage":{"input_tokens":1,"output_tokens":1,"total_tokens":2}}}
    other = copy.deepcopy(first)
    other["response"]["status"] = "failed"
    sse = lambda obj: "data: " + json.dumps(obj) + "\n\n"
    assert meter_sse_text(sse(first) + sse(other)).conflict
    assert not meter_sse_text(sse(first).rstrip()).terminal_seen


async def test_production_rejects_synthetic_quota(service):
    from dataclasses import replace
    service.config = replace(service.config, environment="production")
    data = json.loads((ROOT / "fixtures/telemetry.ok.json").read_text(encoding="utf-8"))
    data["quota"]["source"] = "mock"
    app = create_app(service.config, services=service)
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app), base_url="http://server") as c:
        r = await c.post("/internal/v1/telemetry", json=data, headers={"Authorization": "Bearer tel_synthetic"})
    assert r.status_code == 400 and r.json()["error"]["code"] == "mock_not_allowed"
