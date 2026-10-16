import asyncio
import json
import time
import uuid
from decimal import Decimal
from types import SimpleNamespace

import httpx
import pytest

from codex_relay import api_proxy, auth, key_wallet
from codex_relay.app import create_app
from codex_relay.db import utc_now
from codex_relay.models import NormalizedUsage
from codex_relay.pricing import EstimateResult, insert_price_rule
from codex_relay.stream_meter import StreamMeterResult
from test_server_regression import service


def key_request(service, *, balance="1", stream=True, alias="test"):
    token, key = auth.create_client_key(service.db, service.pepper, alias)
    with service.db.write() as conn:
        key_wallet.set_balance(conn, key.key_id, key_wallet.balance_value(balance))
    ident, attempt = str(uuid.uuid4()), str(uuid.uuid4())
    started = utc_now()
    api_proxy._create_request_rows(service, request_id=ident, attempt_id=attempt, client_key_id=key.key_id,
        client_alias=alias, requested_model="mock-codex", stream=stream, started_at=started)
    return token, key, ident, attempt, started


def estimate(amount):
    return EstimateResult("ok", Decimal(amount), "USD", "test", None)


@pytest.mark.parametrize("value", [True, 1, -1, 1.2, "-1", "NaN", "Infinity", "1000000001", "0.0000001", "x"])
def test_rejects_invalid_balance(value):
    from codex_relay.errors import RelayError
    with pytest.raises(RelayError):
        key_wallet.balance_value(value)


def test_exact_charge_idempotent_rollback_and_overdraft(service):
    _, key, ident, _, _ = key_request(service, balance="0.01")
    usage = NormalizedUsage(status="reported")
    with pytest.raises(RuntimeError):
        with service.db.write() as conn:
            key_wallet.settle(conn, key.key_id, ident, estimate(".0100001"), usage)
            raise RuntimeError("synthetic_rollback")
    assert key_wallet.availability(service.db, key.key_id) is None
    assert service.db.query_one("SELECT balance_usd FROM client_keys WHERE key_id=?", (key.key_id,))[0] == "0.010000"
    with service.db.write() as conn:
        key_wallet.settle(conn, key.key_id, ident, estimate(".0100001"), usage)
        key_wallet.settle(conn, key.key_id, ident, estimate("10"), usage)
    assert service.db.query_one("SELECT balance_usd FROM client_keys WHERE key_id=?", (key.key_id,))[0] == "-1E-7"
    assert service.db.query_one("SELECT COUNT(*) FROM key_balance_entries WHERE kind='charge'")[0] == 1
    assert key_wallet.availability(service.db, key.key_id) == "insufficient_balance"


def test_unknown_usage_blocks_finite_only_and_balance_reset_clears(service):
    _, key, ident, _, _ = key_request(service)
    _, unlimited, other, _, _ = key_request(service, balance=None)
    missing = EstimateResult("unavailable", None, None, None, "no_usage")
    with service.db.write() as conn:
        key_wallet.settle(conn, key.key_id, ident, missing, None)
        key_wallet.settle(conn, unlimited.key_id, other, missing, None)
    assert key_wallet.availability(service.db, key.key_id) == "balance_metering_unavailable"
    assert key_wallet.availability(service.db, unlimited.key_id) is None
    with service.db.write() as conn:
        key_wallet.set_balance(conn, key.key_id, "2.000000")
    assert key_wallet.availability(service.db, key.key_id) is None


def test_metering_and_wallet_commit_once_together(service):
    _, key, ident, attempt, started = key_request(service)
    insert_price_rule(service.db, {"price_version":"test", "model":"mock-codex", "currency":"USD",
        "effective_from":"2020-01-01T00:00:00Z", "prices_per_million":{"input":"2","cached_input":".1","output":"10"}})
    service.reload_prices()
    usage=NormalizedUsage(input_tokens=100000, cached_input_tokens=50000, output_tokens=1000,
        total_tokens=101000, status="reported")
    meter=StreamMeterResult(usage=usage,terminal_seen=True,response_model="mock-codex",terminal_state="completed")
    for _ in range(2):
        api_proxy.persist_result(service,request_id=ident,attempt_id=attempt,client_key_id=key.key_id,
            requested_model="mock-codex",stream=True,http_status=200,response_status="completed",error_code=None,
            error_class=None,started_at=started,started_monotonic=time.monotonic(),first_byte_monotonic=None,
            meter=meter,usage_source="stream",terminal_state="completed")
    row=service.db.query_one("SELECT balance_usd FROM client_keys WHERE key_id=?", (key.key_id,))
    assert Decimal(row[0]) == Decimal(".885")
    assert service.db.query_one("SELECT SUM(request_count) FROM daily_rollups")[0] == 1


def test_concurrent_settlements_do_not_lose_charges_and_retention_keeps_wallet(service):
    from concurrent.futures import ThreadPoolExecutor
    _, key, first, _, started=key_request(service)
    ids=[first,str(uuid.uuid4()),str(uuid.uuid4())]
    for ident in ids[1:]:
        api_proxy._create_request_rows(service,request_id=ident,attempt_id=str(uuid.uuid4()),client_key_id=key.key_id,
            client_alias=key.alias,requested_model="mock-codex",stream=True,started_at=started)
    def charge(ident):
        with service.db.write() as conn:
            key_wallet.settle(conn,key.key_id,ident,estimate(".4"),NormalizedUsage(status="reported"))
    with ThreadPoolExecutor(max_workers=3) as pool:list(pool.map(charge,ids))
    assert Decimal(service.db.query_one("SELECT balance_usd FROM client_keys WHERE key_id=?",(key.key_id,))[0]) == Decimal("-.2")
    service.db.execute("DELETE FROM requests WHERE client_key_id=?",(key.key_id,))
    assert service.db.query_one("SELECT COUNT(*) FROM key_balance_entries WHERE kind='charge'")[0] == 3
    assert key_wallet.availability(service.db,key.key_id) == "insufficient_balance"


def test_restart_recovers_unfinished_finite_wallet_without_affecting_unlimited(service):
    _, key, ident, _, _=key_request(service)
    _, unlimited, other, _, _=key_request(service,balance=None)
    assert key_wallet.recover_pending(service.db)==1
    assert key_wallet.recover_pending(service.db)==0
    assert key_wallet.availability(service.db,key.key_id)=="balance_metering_unavailable"
    assert key_wallet.availability(service.db,unlimited.key_id) is None
    assert service.db.query_one("SELECT ended_at FROM requests WHERE request_id=?",(ident,))[0]
    assert service.db.query_one("SELECT ended_at FROM requests WHERE request_id=?",(other,))[0] is None


async def test_wallet_stops_request_waiting_for_first_headers(service, monkeypatch):
    from codex_relay.errors import RelayError
    _, key, ident, attempt, started=key_request(service)
    entered=asyncio.Event()
    async def transport(request):
        entered.set();await asyncio.sleep(30)
        return httpx.Response(200,json={})
    monkeypatch.setattr(api_proxy,"make_client",lambda _:httpx.AsyncClient(transport=httpx.MockTransport(transport),trust_env=False))
    service.gate.try_acquire(key.key_id)
    pending=asyncio.create_task(api_proxy._handle_stream(service,url="http://synthetic/v1/responses",headers={},body=b'{}',
        request_id=ident,attempt_id=attempt,client_key=key,requested_model="mock-codex",started_at=started,started_monotonic=time.monotonic()))
    await entered.wait()
    with service.db.write() as conn:key_wallet.set_balance(conn,key.key_id,"0.000000")
    with pytest.raises(RelayError) as exc:await asyncio.wait_for(pending,1.5)
    assert exc.value.status_code==402 and service.gate.global_active==0


@pytest.mark.parametrize("stop,status",[("zero",402),("revoke",401),("blocked",503)])
async def test_nonstream_key_stop_has_matching_http_status_in_request_log(service,monkeypatch,stop,status):
    from codex_relay.errors import RelayError
    _, key, ident, attempt, started=key_request(service,stream=False)
    entered=asyncio.Event()
    async def transport(request):
        entered.set();await asyncio.sleep(30)
        return httpx.Response(200,json={})
    monkeypatch.setattr(api_proxy,"make_client",lambda _:httpx.AsyncClient(transport=httpx.MockTransport(transport),trust_env=False))
    service.gate.try_acquire(key.key_id)
    pending=asyncio.create_task(api_proxy._handle_nonstream(service,url="http://synthetic/v1/responses",headers={},body=b'{}',
        request_id=ident,attempt_id=attempt,client_key=key,requested_model="mock-codex",started_at=started,started_monotonic=time.monotonic()))
    await entered.wait()
    if stop=="zero":
        with service.db.write() as conn:key_wallet.set_balance(conn,key.key_id,"0.000000")
    elif stop=="revoke":auth.revoke_client_key(service.db,key.key_id)
    else:service.db.execute("UPDATE client_keys SET balance_blocked=1 WHERE key_id=?",(key.key_id,))
    with pytest.raises(RelayError) as exc:await asyncio.wait_for(pending,1.5)
    assert exc.value.status_code==status and service.gate.global_active==0
    assert service.db.query_one("SELECT http_status FROM requests WHERE request_id=?",(ident,))[0]==status


@pytest.mark.parametrize("status",[200,429])
async def test_nonstream_unknown_usage_requires_review_but_rejection_does_not(service,monkeypatch,status):
    from codex_relay.errors import RelayError
    _, key, ident, attempt, started=key_request(service,stream=False)
    monkeypatch.setattr(api_proxy,"make_client",lambda _:httpx.AsyncClient(transport=httpx.MockTransport(
        lambda r:httpx.Response(status,json={"id":"synthetic","status":"completed"})),trust_env=False))
    service.gate.try_acquire(key.key_id)
    call=api_proxy._handle_nonstream(service,url="http://synthetic/v1/responses",headers={},body=b'{}',
        request_id=ident,attempt_id=attempt,client_key=key,requested_model="mock-codex",started_at=started,started_monotonic=time.monotonic())
    if status==200:
        assert (await call).status_code==200
        assert key_wallet.availability(service.db,key.key_id)=="balance_metering_unavailable"
    else:
        with pytest.raises(RelayError):await call
        assert key_wallet.availability(service.db,key.key_id) is None
    assert service.gate.global_active==0


async def admin_client(service):
    token, session = auth.create_session(service.db, service.session_secret, "admin", 60)
    client = httpx.AsyncClient(transport=httpx.ASGITransport(create_app(service.config, services=service)), base_url="http://test")
    client.cookies.set("relay_session", token)
    client.headers["x-relay-csrf"] = auth.csrf_token_for(session, service.session_secret)
    return client


async def test_admin_create_edit_balance_csrf_and_gateway_402(service):
    async with await admin_client(service) as client:
        response = await client.post("/admin/api/client-keys", json={"alias":"named", "balance_usd":"0"})
        assert response.status_code == 201
        body=response.json(); token, ident = body["plaintext_key"],body["key_id"]
        header={"Authorization":"Bearer "+token}
        r=await client.get("/v1/models",headers=header)
        assert r.status_code == 402 and r.json()["error"]["code"] == "insufficient_balance"
        listing=(await client.get("/admin/api/client-keys")).json()["items"]
        assert listing[0]["balance_usd"] == "0.000000" and "digest" not in listing[0]
        assert token not in json.dumps(listing)
        path="/admin/api/client-keys/"+ident
        assert (await client.patch(path,json={"alias":"renamed"},headers={"x-relay-csrf":"bad"})).status_code == 403
        assert (await client.patch(path,json={"alias":"renamed"})).status_code == 200
        assert (await client.patch(path,json={"balance_usd":None})).status_code == 200
        assert key_wallet.availability(service.db,ident) is None
        assert auth.lookup_client_key(service.db,service.pepper,token).alias == "renamed"
        duplicate=await client.post("/admin/api/client-keys",json={"alias":"RENAMED"})
        assert duplicate.status_code == 400
        assert (await client.patch(path,json={"balance_usd":"NaN"})).status_code == 400
        assert (await client.delete(path)).status_code == 200
        assert (await client.get("/v1/models",headers=header)).status_code == 401
        assert (await client.patch(path,json={"balance_usd":"2"})).status_code == 400
        assert (await client.get("/keys")).status_code == 200
        records=(await client.get("/admin/api/connection-events",params={"client_key_id":ident})).json()["items"]
        assert any(r.get("error_code")=="insufficient_balance" for r in records)


@pytest.mark.parametrize("stop", ["zero", "revoke", "other", "blocked"])
async def test_silent_stream_wallet_stop_closes_upstream_and_releases_once(service, monkeypatch, stop):
    _, key, ident, attempt, started=key_request(service)
    _, other, _, _, _=key_request(service,alias="other")
    class Silent(httpx.AsyncByteStream):
        closed=False
        async def __aiter__(self):
            yield b'data: {"type":"response.created"}\n\n'
            await asyncio.sleep(30)
        async def aclose(self): self.closed=True
    source=Silent()
    monkeypatch.setattr(api_proxy,"make_client",lambda _:httpx.AsyncClient(
        transport=httpx.MockTransport(lambda r:httpx.Response(200,stream=source)),trust_env=False))
    service.gate.try_acquire(key.key_id)
    response=await api_proxy._handle_stream(service,url="http://synthetic/v1/responses",headers={},body=b'{}',
        request_id=ident,attempt_id=attempt,client_key=key,requested_model="mock-codex",started_at=started,started_monotonic=time.monotonic())
    received=asyncio.Event()
    async def consume():
        async for _ in response.body_iterator:received.set()
    task=asyncio.create_task(consume())
    await received.wait()
    if stop=="revoke": auth.revoke_client_key(service.db,key.key_id)
    elif stop=="blocked":service.db.execute("UPDATE client_keys SET balance_blocked=1 WHERE key_id=?",(key.key_id,))
    else:
        with service.db.write() as conn:key_wallet.set_balance(conn,other.key_id if stop=="other" else key.key_id,"0.000000")
    if stop=="other":
        await asyncio.sleep(.65)
        assert not task.done()
        task.cancel()
        with pytest.raises(asyncio.CancelledError):await task
    else:
        with pytest.raises(key_wallet.KeyStopped):await asyncio.wait_for(task,1.5)
    await response.background()
    assert source.closed and service.gate.global_active == 0
    row=service.db.query_one("SELECT response_status,error_code FROM requests WHERE request_id=?",(ident,))
    assert row["response_status"]=="interrupted"
    if stop=="zero":assert row["error_code"]=="insufficient_balance"
    if stop=="revoke":assert row["error_code"]=="invalid_api_key"


async def test_filters_current_name_costs_and_csv_key_isolation(service):
    _, a, request_a, _, _=key_request(service,alias="old")
    _, b, request_b, _, _=key_request(service,alias="second")
    for ident in (request_a,request_b):
        service.db.execute("UPDATE requests SET estimated_cost='1.234567',estimate_status='ok',currency='USD',"
                           "total_tokens=42,usage_status='reported',response_status='completed' WHERE request_id=?",(ident,))
    async with await admin_client(service) as client:
        assert (await client.patch('/admin/api/client-keys/'+a.key_id,json={"alias":"current"})).status_code==200
        query={"client_key_id":a.key_id}
        data=(await client.get('/admin/api/requests',params=query)).json()
        assert data['total']==1 and data['items'][0]['client_alias']=='current'
        csv=(await client.get('/admin/api/requests.csv',params=query)).text
        assert request_a in csv and request_b not in csv and 'current' in csv
        cost=(await client.get('/admin/api/key-usage',params=query)).json()
        assert cost['cost_usd']=='1.234567' and cost['total_tokens']==42 and cost['wallet']['alias']=='current'
        all_cost=(await client.get('/admin/api/key-usage')).json()
        assert all_cost['cost_usd']=='2.469134' and all_cost['wallet'] is None
        assert (await client.get('/admin/api/key-usage',params={"client_key_id":"missing"})).status_code==400
    async with httpx.AsyncClient(transport=httpx.ASGITransport(create_app(service.config,services=service)),base_url="http://test") as client:
        for path in ('client-keys','key-usage','connection-events'):
            assert (await client.get('/admin/api/'+path)).status_code==401


def test_additive_migration_preserves_old_credentials_and_sets_unlimited(tmp_path):
    import sqlite3
    from pathlib import Path
    from codex_relay.db import Database
    path=tmp_path/'old.db'
    conn=sqlite3.connect(path)
    conn.executescript((Path(__file__).parents[1]/'server/codex_relay/migrations/0001_init.sql').read_text(encoding='utf-8'))
    conn.execute("CREATE TABLE schema_migrations(version INTEGER PRIMARY KEY,name TEXT NOT NULL,applied_at TEXT NOT NULL)")
    conn.execute("INSERT INTO schema_migrations VALUES(1,'0001_init.sql',?)", (utc_now(),))
    conn.execute("INSERT INTO client_keys(key_id,alias,digest,created_at) VALUES('old','previous','synthetic_digest',?)",(utc_now(),))
    conn.commit();conn.close()
    db=Database(path);db.migrate();db.migrate()
    row=db.query_one("SELECT alias,digest,balance_usd FROM client_keys WHERE key_id='old'")
    assert tuple(row)==('previous','synthetic_digest',None)
    db.close()
