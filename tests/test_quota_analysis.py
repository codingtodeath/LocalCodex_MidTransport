import json
from datetime import UTC, datetime, timedelta
from decimal import Decimal

import httpx
import pytest

from codex_relay.db import Database, iso
from codex_relay import auth
from codex_relay.quota_analysis import analyze, connection, create_app
from codex_relay.pricing import insert_price_rule


NOW = datetime(2026, 10, 4, 1, tzinfo=UTC)
START = datetime(2026, 10, 4, tzinfo=UTC)
SECRET = b"synthetic-session-secret-32-bytes!"


def at(seconds):
    return iso(START + timedelta(seconds=seconds))


def snapshot(db, second, used, *, reset="2026-10-11T00:00:00.000Z", epoch="one", verified=True):
    windows = [{"bucket_id": "account", "window_id": "primary", "window_minutes": 10080,
                "used_percent": used, "resets_at": reset}]
    db.execute("""INSERT INTO quota_snapshots(worker_id,account_epoch,identity_check,status,source,
        captured_at,received_at,windows_json) VALUES(?, ?, ?, 'ok', 'codex_app_server', ?, ?, ?)""",
        ("home", epoch, "automatic_match" if verified else "unknown", at(second), at(second), json.dumps(windows)))


def request(db, id="req", *, start=30, end=90, cached=200000, write=100000, model="sample", status="reported", conflict=0):
    db.execute("""INSERT INTO requests(request_id,worker_id,started_at,ended_at,created_at,requested_model,
        input_tokens,cached_input_tokens,cache_write_tokens,output_tokens,total_tokens,usage_status,usage_conflict,
        estimated_cost,estimate_status,currency) VALUES(?, 'home', ?, ?, ?, ?, 1000000, ?, ?, 1000, 1001000, ?, ?, '99', 'ok', 'USD')""",
        (id, at(start), at(end) if end is not None else None, at(start), model, cached, write, status, conflict))


def price(db, model="sample", currency="USD", effective=None):
    insert_price_rule(db, {"price_version": "test-v1", "model": model, "currency": currency,
        "effective_from": effective or at(-100), "service_tier": "standard", "cache_write_policy": "separate",
        "prices_per_million": {"input": "2", "cached_input": ".1", "cache_write": "2.5", "output": "10"}})


@pytest.fixture
def analysis_db(tmp_path):
    db = Database(tmp_path / "relay.db"); db.migrate()
    price(db); snapshot(db, 0, 10); snapshot(db, 120, 11); snapshot(db, 240, 13)
    request(db)
    yield db
    db.close()


def run(db, confirmed=True, start=None, end=None):
    with connection(db.path) as c:
        return analyze(c, "home", start or at(-60), end or at(300), confirmed, now=NOW)


def test_aligns_to_snapshots_and_cost_partition_is_nonadditive_readonly(analysis_db):
    db = analysis_db
    result = run(db)
    assert result["effective_start"] == at(0) and result["effective_end"] == at(240)
    assert result["start_trim_seconds"] == result["end_trim_seconds"] == 60
    assert result["cost"]["min"] == result["cost"]["max"] == "1.680000"
    assert result["cost"]["input_tokens"] == 1000000 and result["cost"]["total_tokens"] == 1001000
    window = result["windows"][0]
    assert window["observed_used_points"] == 3 and window["ratio_available"]
    assert window["cost_per_point_min"] == "0.560000"
    assert db.query_one("SELECT estimated_cost FROM requests")[0] == "99"
    with connection(db.path) as c:
        with pytest.raises(Exception): c.execute("UPDATE requests SET estimated_cost='0'")


def test_confirmation_is_required_but_usage_still_visible(analysis_db):
    result = run(analysis_db, confirmed=False)
    assert result["cost"]["min"] == "1.680000"
    assert not result["windows"][0]["ratio_available"]
    assert result["windows"][0]["cost_per_point_min"] is None


@pytest.mark.parametrize("problem", ["gap", "reset", "rollback", "identity", "unverified", "zero"])
def test_uncertain_quota_does_not_get_a_ratio(analysis_db, problem):
    db = analysis_db
    if problem == "gap": snapshot(db, 1200, 14)
    elif problem == "reset": snapshot(db, 300, 1, reset="2026-10-18T00:00:00.000Z")
    elif problem == "rollback": snapshot(db, 300, 1)
    elif problem == "identity": snapshot(db, 300, 14, epoch="different")
    elif problem == "unverified": snapshot(db, 300, 14, verified=False)
    else: db.execute("UPDATE quota_snapshots SET windows_json=replace(windows_json, '\"used_percent\": 11', '\"used_percent\": 10')"); db.execute("UPDATE quota_snapshots SET windows_json=replace(windows_json, '\"used_percent\": 13', '\"used_percent\": 10')")
    result = run(db, end=at(1300))
    assert not result["windows"][0]["ratio_available"]
    assert result["windows"][0]["cost_per_point_min"] is None


@pytest.mark.parametrize("start,end", [(-30, 30), (230, 270), (200, None)])
def test_cross_boundary_and_running_requests_suppress_ratio(analysis_db, start, end):
    request(analysis_db, "boundary", start=start, end=end)
    result = run(analysis_db)
    assert result["cost"]["crossing_requests"] == 1
    assert result["cost"]["requests"] == 1
    assert not result["windows"][0]["ratio_available"]


def test_unknown_write_volume_gets_bounds_and_missing_usage_is_not_zero(analysis_db):
    db = analysis_db
    db.execute("UPDATE requests SET cache_write_tokens=NULL")
    result = run(db)
    assert result["cost"]["min"] == "1.630000" and result["cost"]["max"] == "2.030000"
    assert result["cost"]["uncertain_write_requests"] == 1
    assert result["windows"][0]["ratio_available"]
    db.execute("UPDATE requests SET cached_input_tokens=NULL")
    result = run(db)
    assert result["cost"]["min"] is None and result["cost"]["unpriced_requests"] == 1
    assert not result["windows"][0]["ratio_available"]


def test_price_effectivity_and_mixed_currency(analysis_db):
    db = analysis_db
    db.execute("UPDATE price_rules SET effective_from=?", (at(45),))
    assert run(db)["cost"]["min"] is None
    db.execute("UPDATE price_rules SET effective_from=?", (at(-100),))
    price(db, model="other", currency="EUR");request(db, "other", model="other")
    result = run(db)
    assert result["cost"]["currency"] is None and result["cost"]["min"] is None
    assert not result["windows"][0]["ratio_available"]


def test_not_enough_snapshots_and_invalid_intervals(analysis_db):
    assert run(analysis_db, start=at(121), end=at(300))["issues"] == ["not_enough_snapshots"]
    from fastapi import HTTPException
    for start, end in [(at(300), at(0)), ("2026-10-04T00:00", at(300)), (at(-32*86400), at(300)), (at(0), at(4000))]:
        with pytest.raises(HTTPException): run(analysis_db, start=start, end=end)


def test_key_filter_changes_cost_only_and_suppresses_account_ratio(analysis_db):
    db = analysis_db
    db.execute("UPDATE requests SET client_key_id='first'")
    request(db, "other")
    db.execute("UPDATE requests SET client_key_id='second' WHERE request_id='other'")
    with connection(db.path) as c:
        result = analyze(c, "home", at(-60), at(300), True, now=NOW, client_key_id="first")
    assert result["cost"]["requests"] == 1 and result["cost"]["min"] == "1.680000"
    assert result["windows"][0]["observed_used_points"] == 3
    assert not result["windows"][0]["ratio_available"]
    assert "key_quota_not_attributable" in result["issues"]


async def test_endpoint_requires_live_admin_cookie_and_never_touches_session(analysis_db):
    db = analysis_db
    token, _ = auth.create_session(db, SECRET, "admin", 60)
    before = db.query_one("SELECT last_seen_at FROM admin_sessions")[0]
    app = create_app(db.path, "home", SECRET)
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
        path = "/admin/api/quota-analysis?start=" + at(0) + "&end=" + at(300)
        assert (await client.get(path)).status_code == 401
        assert (await client.get(path, headers={"Authorization": "Bearer gw_synthetic"})).status_code == 401
        client.cookies.set("relay_session", token)
        response = await client.get(path)
        assert response.status_code == 200 and response.headers["cache-control"] == "no-store"
        assert not response.json()["windows"][0]["ratio_available"]
        assert db.query_one("SELECT last_seen_at FROM admin_sessions")[0] == before
        assert (await client.post(path)).status_code == 405
        assert (await client.get("/public/api/quota-analysis")).status_code == 404
        auth.revoke_session(db, SECRET, token)
        assert (await client.get(path)).status_code == 401


def test_multiple_quota_windows_are_not_added_together(analysis_db):
    db = analysis_db
    for row in db.query("SELECT snapshot_id,windows_json FROM quota_snapshots ORDER BY captured_at"):
        ws = json.loads(row["windows_json"])
        ws.append({**ws[0], "window_id": "secondary", "window_minutes": 300,
                   "used_percent": ws[0]["used_percent"] * 2})
        db.execute("UPDATE quota_snapshots SET windows_json=? WHERE snapshot_id=?",
                   (json.dumps(ws), row["snapshot_id"]))
    result = run(db)
    windows = {w["window_id"]: w for w in result["windows"]}
    assert windows["primary"]["observed_used_points"] == 3
    assert windows["secondary"]["observed_used_points"] == 6
    assert windows["primary"]["cost_per_point_min"] == "0.560000"
    assert windows["secondary"]["cost_per_point_min"] == "0.280000"
