import json
from datetime import UTC, datetime

import httpx
import pytest

from codex_relay.db import Database
from codex_relay.public_dashboard import aggregate, bounds, create_public_app, processing_snapshot
from codex_relay import worker_state
from datetime import timedelta

NOW = datetime(2026, 10, 4, 6, 30, tzinfo=UTC)  # 14:30 UTC+8


def ready(db, now):
    worker_state.record_heartbeat(db, worker_id='home', instance_id='private-instance', seq=1,
        sent_at=now.isoformat(), capabilities={}, local={'adapter_ready':True,'auth_state':'ready'}, now=now)
    worker_state.record_probe(db, 'home', ok=True, instance_id='private-instance', now=now)


def pending(db, ident, at):
    db.execute('INSERT INTO requests(request_id,worker_id,started_at,created_at,client_alias,requested_model) VALUES(?,?,?,?,?,?)',
        (ident,'home',at,at,'private-key-name','private-model'))


def test_processing_counts_live_rows_and_excludes_stale_or_finished(dashboard_db,tmp_path):
    db=dashboard_db;ready(db,NOW)
    pending(db,'live',(NOW-timedelta(seconds=10)).isoformat())
    pending(db,'finished',(NOW-timedelta(seconds=11)).isoformat())
    db.execute('UPDATE requests SET ended_at=? WHERE request_id=?',(NOW.isoformat(),'finished'))
    pending(db,'previous-gateway',(NOW-timedelta(seconds=180)).isoformat())
    db.execute("INSERT INTO audit_events(at,category,action) VALUES(?,'service','started')",((NOW-timedelta(seconds=120)).isoformat(),))
    limits=tmp_path/'limits.json';limits.write_text('{"global_limit":4}')
    data=processing_snapshot(db.path,'home',2,now=NOW,limits_path=limits)
    assert data['available'] and data['active_requests']==1 and data['limit']==4
    assert all(secret not in json.dumps(data) for secret in ['private-key-name','private-model','private-instance','previous-gateway'])
    unavailable=processing_snapshot(db.path,'home',4,now=NOW+timedelta(minutes=2))
    assert unavailable['active_requests'] is None and not unavailable['available']


async def test_live_processing_bypasses_summary_cache_and_never_writes(dashboard_db,tmp_path):
    db=dashboard_db;now=datetime.now(UTC);ready(db,now)
    pending(db,'private-request-id',(now-timedelta(seconds=1)).isoformat())
    limits=tmp_path/'limits.json';limits.write_text('{"global_limit":4}')
    app=create_public_app(db.path,'home',concurrency_limit=4,limits_path=limits)
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app),base_url='http://test') as client:
        await client.get('/public/api/overview?range=7d')  # Populate historic cache.
        before=db.query_one('PRAGMA data_version')[0]
        r=await client.get('/public/api/overview?live=true')
        assert r.status_code==200 and r.headers['cache-control']=='no-store'
        assert set(r.json())=={'processing'} and r.json()['processing']['active_requests']==1
        assert 'private' not in r.text
        assert db.query_one('PRAGMA data_version')[0]==before
        db.execute('UPDATE requests SET ended_at=? WHERE request_id=?',(datetime.now(UTC).isoformat(),'private-request-id'))
        limits.write_text('{"global_limit":5}')
        data=(await client.get('/public/api/overview?live=true')).json()['processing']
        assert data['active_requests']==0 and data['limit']==5


@pytest.fixture
def dashboard_db(tmp_path):
    db = Database(tmp_path / "relay.db")
    db.migrate()
    yield db
    db.close()


def request(db, id, at, tokens=10, status="reported", conflict=0):
    db.execute("""INSERT INTO requests(request_id,started_at,created_at,usage_status,
               total_tokens,input_tokens,output_tokens,usage_conflict,client_alias,requested_model)
               VALUES(?,?,?,?,?,?,?,?,?,?)""",
               (id, at, at, status, tokens, tokens - 2, 2, conflict, "private-client", "private-model"))


def snapshot(db, at, used, reset="2026-10-11T00:00:00.000Z", epoch="epoch", current=0):
    windows = json.dumps([dict(bucket_id="private-bucket", window_id="primary", used_percent=used,
                               window_minutes=10080, resets_at=reset)])
    db.execute("""INSERT INTO quota_snapshots(worker_id,account_epoch,identity_check,status,source,
              captured_at,received_at,windows_json,is_current,account_alias)
              VALUES(?,?,'automatic_match','ok','codex_app_server',?,?,?,?,?)""",
              ("home", epoch, at, at, windows, current, "private-account"))


def test_utc8_midnight_and_usage_conflicts(dashboard_db):
    db = dashboard_db
    request(db, "before", "2026-10-03T15:59:59.000Z", 100)
    request(db, "midnight", "2026-10-03T16:00:00.000Z", 10)
    request(db, "one", "2026-10-03T17:00:00.000Z", 20)
    request(db, "missing", "2026-10-03T17:01:00.000Z", 999, "missing")
    request(db, "conflict", "2026-10-03T17:02:00.000Z", 999, conflict=1)
    result = aggregate(db.path, "home", "today", now=NOW)
    assert result["totals"] == dict(requests=4, reported_requests=2, total_tokens=30,
                                    input_tokens=26, output_tokens=4, unknown_requests=2)
    assert result["hourly"][0]["total_tokens"] == 10
    assert result["hourly"][1]["total_tokens"] == 20
    assert result["recorded_total"]["total_tokens"] == 130
    assert result["quota_windows"] == []
    assert "private-client" not in json.dumps(result)
    assert "private-model" not in json.dumps(result)
    with pytest.raises(ValueError):
        bounds("arbitrary", NOW)


def test_quota_gap_reset_epoch_and_unknown_are_not_zero(dashboard_db):
    db = dashboard_db
    snapshot(db, "2026-10-04T03:55:00.000Z", 99, epoch="old")
    snapshot(db, "2026-10-04T04:00:00.000Z", 10)
    snapshot(db, "2026-10-04T04:05:00.000Z", 13)
    snapshot(db, "2026-10-04T04:10:00.000Z", 13)
    snapshot(db, "2026-10-04T05:00:00.000Z", 20)  # Gap: don't infer seven points.
    snapshot(db, "2026-10-04T05:05:00.000Z", 0, reset="2026-10-18T00:00:00.000Z")
    snapshot(db, "2026-10-04T05:10:00.000Z", 2, reset="2026-10-18T00:00:00.000Z", current=1)
    result = aggregate(db.path, "home", "today", now=NOW)
    window = result["quota_windows"][0]
    assert window["observed_used_points"] == 5
    assert window["hourly_used_points"][12:14] == [3, 2]
    assert window["hourly_used_points"][11] is None
    assert window["gap_count"] == window["reset_count"] == 1
    assert window["fresh"] is False
    assert window["coverage_minutes"][12] == 10
    for secret in ("private-bucket", "private-account", "epoch"):
        assert secret not in json.dumps(result)


def test_zero_is_observed_only_with_valid_pair(dashboard_db):
    snapshot(dashboard_db, "2026-10-04T06:20:00.000Z", 5)
    snapshot(dashboard_db, "2026-10-04T06:25:00.000Z", 5, current=1)
    result = aggregate(dashboard_db.path, "home", "today", now=NOW)
    window = result["quota_windows"][0]
    assert window["observed_used_points"] == 0
    assert window["fresh"] is True
    assert window["hourly_used_points"][14] == 0
    assert window["hourly_used_points"][13] is None
    assert len(aggregate(dashboard_db.path, "home", "7d", now=NOW)["hourly"]) == 168
    assert len(aggregate(dashboard_db.path, "home", "30d", now=NOW)["hourly"]) == 720


def test_reset_estimate_jitter_and_past_reset_never_refill(dashboard_db):
    snapshot(dashboard_db, "2026-10-04T06:20:00.000Z", 5, reset="2026-10-04T06:29:00.000Z")
    snapshot(dashboard_db, "2026-10-04T06:25:00.000Z", 6, reset="2026-10-04T06:29:01.000Z", current=1)
    window = aggregate(dashboard_db.path, "home", "today", now=NOW)["quota_windows"][0]
    assert window["observed_used_points"] == 1
    assert window["reset_count"] == 0
    assert window["remaining_percent"] == 94
    assert window["reset_pending_confirmation"] is True


@pytest.mark.asyncio
async def test_public_api_has_no_private_routes_or_writes(dashboard_db):
    before = dashboard_db.query_one("PRAGMA data_version")[0]
    app = create_public_app(dashboard_db.path, "home")
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
        response = await client.get("/public/api/overview?range=7d")
        assert response.status_code == 200
        assert response.headers["cache-control"] == "no-store"
        for path in ("/admin/api/quota", "/public/api/requests", "/docs", "/openapi.json"):
            assert (await client.get(path)).status_code == 404
        assert (await client.post("/public/api/overview")).status_code == 405
        assert (await client.get("/public/api/overview?range=invalid")).status_code == 400
    assert dashboard_db.query_one("PRAGMA data_version")[0] == before


def test_public_relay_status_only_reports_heartbeat_and_tunnel(dashboard_db):
    db = dashboard_db
    worker_state.record_heartbeat(db, worker_id="home", instance_id="private-instance", seq=1,
                                 sent_at=NOW.isoformat(), capabilities={},
                                 local={"adapter_ready": True, "auth_state": "ready"}, now=NOW)
    worker_state.record_probe(db, "home", ok=True, instance_id="private-instance", now=NOW)
    result = aggregate(db.path, "home", "today", now=NOW)
    assert result["relay"] == dict(state="online", connected=True, heartbeat_ok=True, tunnel_ok=True)
    assert "private-instance" not in json.dumps(result)
    worker_state.record_heartbeat(db, worker_id="home", instance_id="private-instance", seq=2,
                                 sent_at=NOW.isoformat(), capabilities={},
                                 local={"adapter_ready": False, "auth_state": "ready"}, now=NOW)
    outage = aggregate(db.path, "home", "today", now=NOW)["relay"]
    assert outage == dict(state="online", connected=True, heartbeat_ok=True, tunnel_ok=True)
    worker_state.record_heartbeat(db, worker_id="home", instance_id="private-instance", seq=3,
                                 sent_at=NOW.isoformat(), capabilities={},
                                 local={"adapter_ready": True, "auth_state": "ready"}, now=NOW)
    assert aggregate(db.path, "home", "today", now=NOW)["relay"]["connected"]
    worker_state.record_heartbeat(db, worker_id="home", instance_id="private-instance", seq=4,
                                 sent_at=NOW.isoformat(), capabilities={},
                                 local={"adapter_ready": True, "auth_state": "reauth_required"}, now=NOW)
    assert aggregate(db.path, "home", "today", now=NOW)["relay"]["state"] == "online"
    worker_state.record_probe(db, 'home', ok=True, instance_id='other-instance', now=NOW)
    assert aggregate(db.path, 'home', 'today', now=NOW)['relay']['state'] == 'degraded'
    assert aggregate(db.path, "home", "today", now=NOW + timedelta(minutes=2))["relay"]["state"] == "offline"
