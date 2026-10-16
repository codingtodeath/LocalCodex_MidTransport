"""Public, aggregate-only dashboard sidecar. Never writes to the relay database.

Run separately so deployment does not restart inference or interrupt streams.
Quota deltas are observed percentage points, not a token-to-subscription formula.
"""
from __future__ import annotations

import json
import math
import sqlite3
import threading
import time
from contextlib import closing
from datetime import UTC, datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace

from fastapi import FastAPI, HTTPException
from fastapi.responses import FileResponse, JSONResponse

from .db import parse_iso
from . import worker_state

TZ = timezone(timedelta(hours=8))
RANGES = {"today": 1, "yesterday": 1, "7d": 7, "30d": 30}


def bounds(period: str, now: datetime):
    if period not in RANGES:
        raise ValueError("unsupported_range")
    day = now.astimezone(TZ).replace(hour=0, minute=0, second=0, microsecond=0)
    end = day if period == "yesterday" else day + timedelta(days=1)
    start = day - timedelta(days=1) if period == "yesterday" else end - timedelta(days=RANGES[period])
    return start, end


def stamp(value: datetime) -> str:
    return value.astimezone(UTC).isoformat(timespec="milliseconds").replace("+00:00", "Z")


def window_name(minutes):
    if not minutes:
        return "额度窗口"
    if minutes % 1440 == 0:
        return f"{minutes // 1440} 天窗口"
    return f"{minutes / 60:g} 小时窗口"


def aggregate(path: Path, worker: str, period: str, *, now=None, stale_seconds=300,
              heartbeat_stale_seconds=35, probe_interval_seconds=15):
    now = now or datetime.now(UTC)
    start, end = bounds(period, now)
    count = int((end - start).total_seconds() // 3600)
    buckets = [{"at": stamp(start + timedelta(hours=i)), "requests": 0,
                "total_tokens": 0, "input_tokens": 0, "output_tokens": 0,
                "reported_requests": 0} for i in range(count)]
    # mode=ro, query_only, short-lived read transactions; no migration/PRAGMA WAL writes.
    conn = sqlite3.connect(path.resolve().as_uri() + "?mode=ro", uri=True, timeout=1)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA query_only=ON")
    deadline = time.monotonic() + 3
    conn.set_progress_handler(lambda: int(time.monotonic() > deadline), 1000)
    try:
        reader = SimpleNamespace(query_one=lambda sql, params=(): conn.execute(sql, params).fetchone())
        snapshot = worker_state.evaluate(reader, worker,
            SimpleNamespace(heartbeat_stale_seconds=heartbeat_stale_seconds,
                            probe_interval_seconds=probe_interval_seconds), now=now)
        connected = snapshot.heartbeat_fresh and snapshot.probe_fresh and snapshot.instance_consistent
        status = "online" if connected else ("degraded" if snapshot.heartbeat_fresh else snapshot.state)
        relay = {"state": status, "connected": connected,
                 "heartbeat_ok": snapshot.heartbeat_fresh,
                 "tunnel_ok": snapshot.probe_fresh and snapshot.instance_consistent}
        rows = conn.execute("""
            SELECT CAST((julianday(started_at)-julianday(?))*24 + 0.000001 AS INTEGER) AS h,
                   COUNT(*) AS n,
                   SUM(CASE WHEN usage_status='reported' AND usage_conflict=0 THEN 1 ELSE 0 END) AS reported,
                   SUM(CASE WHEN usage_status='reported' AND usage_conflict=0 THEN COALESCE(total_tokens,0) ELSE 0 END) AS tokens,
                   SUM(CASE WHEN usage_status='reported' AND usage_conflict=0 THEN COALESCE(input_tokens,0) ELSE 0 END) AS input,
                   SUM(CASE WHEN usage_status='reported' AND usage_conflict=0 THEN COALESCE(output_tokens,0) ELSE 0 END) AS output
            FROM requests WHERE started_at>=? AND started_at<? GROUP BY h
        """, (stamp(start), stamp(start), stamp(min(end, now)))).fetchall()
        for row in rows:
            if 0 <= row["h"] < count:
                buckets[row["h"]].update(requests=row["n"], reported_requests=row["reported"],
                                         total_tokens=row["tokens"], input_tokens=row["input"], output_tokens=row["output"])
        lifetime = conn.execute("""SELECT COUNT(*) AS requests,
            SUM(CASE WHEN usage_status='reported' AND usage_conflict=0 THEN COALESCE(total_tokens,0) ELSE 0 END) AS total_tokens,
            MIN(started_at) AS since FROM requests"""
        ).fetchone()
        latest = conn.execute("""SELECT captured_at,windows_json,account_epoch,identity_check
            FROM quota_snapshots WHERE worker_id=? AND is_current=1 ORDER BY captured_at DESC LIMIT 1""", (worker,)).fetchone()
        snapshots = []
        if latest and latest["identity_check"] in ("automatic_match", "manual_confirmed"):
            snapshots = conn.execute("""SELECT captured_at,windows_json,account_epoch FROM quota_snapshots
                WHERE worker_id=? AND status='ok' AND identity_check IN ('automatic_match','manual_confirmed')
                AND account_epoch IS ? AND captured_at>=? AND captured_at<=?
                ORDER BY captured_at LIMIT 50000""",
                (worker, latest["account_epoch"], stamp(start - timedelta(minutes=15)), stamp(min(end, now)))).fetchall()
    finally:
        conn.close()

    windows = []
    latest_windows = json.loads(latest["windows_json"]) if latest and latest["identity_check"] in ("automatic_match", "manual_confirmed") else []
    for index, current in enumerate(latest_windows):
        key = (current["bucket_id"], current["window_id"])
        points = [None] * count
        coverage = [0.] * count
        previous = None
        gaps = resets = 0
        for row in snapshots:
            captured = parse_iso(row["captured_at"])
            item = next((w for w in json.loads(row["windows_json"]) if (w["bucket_id"], w["window_id"]) == key), None)
            if not item or captured is None:
                previous = None
                continue
            used = float(item["used_percent"])
            if not math.isfinite(used):
                previous = None
                continue
            if previous:
                prev_at, prev_item = previous
                seconds = (captured - prev_at).total_seconds()
                delta = used - float(prev_item["used_percent"])
                h = int((captured - start).total_seconds() // 3600)
                reset = parse_iso(item.get("resets_at"))
                previous_reset = parse_iso(prev_item.get("resets_at"))
                # Upstream reset estimates sometimes jitter by one second; this is not a new cycle.
                same_cycle = (reset is not None and previous_reset is not None
                              and abs((reset - previous_reset).total_seconds()) <= 5
                              and item.get("window_minutes") == prev_item.get("window_minutes"))
                if 0 <= h < count:
                    if not same_cycle or delta < 0:
                        resets += 1
                    elif 0 < seconds <= 900:
                        points[h] = (points[h] or 0.) + delta
                        # Only elapsed time inside this bucket counts toward its coverage.
                        coverage[h] += max(0., (captured - max(prev_at, start + timedelta(hours=h))).total_seconds() / 60)
                    elif seconds > 900:
                        gaps += 1
            previous = captured, item
        age = (now - parse_iso(latest["captured_at"])).total_seconds()
        reset = parse_iso(current.get("resets_at"))
        windows.append({"id": str(index), "label": window_name(current.get("window_minutes")),
                        "current_used_percent": current["used_percent"],
                        "remaining_percent": max(0, min(100, 100 - current["used_percent"])),
                        "resets_at": current.get("resets_at"),
                        "reset_pending_confirmation": reset is not None and reset <= now,
                        "fresh": -60 <= age <= stale_seconds,
                        "observed_used_points": round(sum(p for p in points if p is not None), 4) if any(p is not None for p in points) else None,
                        "hourly_used_points": [round(p, 4) if p is not None else None for p in points],
                        "coverage_minutes": [round(min(60., m), 2) for m in coverage],
                        "gap_count": gaps, "reset_count": resets})
    total = {k: sum(b[k] for b in buckets) for k in ("requests", "reported_requests", "total_tokens", "input_tokens", "output_tokens")}
    total["unknown_requests"] = total["requests"] - total["reported_requests"]
    return {"range": period, "timezone": "Asia/Hong_Kong", "start": stamp(start), "end": stamp(end),
            "generated_at": stamp(now), "relay": relay, "hourly": buckets, "totals": total,
            "recorded_total": {"requests": lifetime["requests"], "total_tokens": lifetime["total_tokens"] or 0,
                               "since": lifetime["since"]},
            "quota_windows": windows,
            "quota_captured_at": latest["captured_at"] if windows else None,
            "notes": {"tokens": "本网关上游已报告 Token；缺失或冲突的用量不计入。累计仅覆盖服务器保留的流水。",
                      "quota": "账号级快照的已用比例增量，包含其他 Codex 设备消耗。按后一次快照所在小时计入；采样间隔超过 15 分钟、比例回退或窗口重置不推算。",
                      "points": "累计百分点可跨多个周期超过 100；不代表当前剩余额度，不与 Token 换算。"}}


def processing_snapshot(path: Path, worker: str, limit: int, *, limits_path=None,
                        now=None, request_deadline_seconds=1800,
                        heartbeat_stale_seconds=35, probe_interval_seconds=15):
    now = now or datetime.now(UTC)
    if limits_path:
        try:
            value = json.loads(Path(limits_path).read_text()).get('global_limit')
            if type(value) is int and 1 <= value <= 100:
                limit = value
        except (OSError, ValueError, TypeError, AttributeError):
            pass
    data = {'active_requests': None, 'limit': limit, 'observed_at': stamp(now),
            'available': False, 'basis': 'gateway_inflight_requests'}
    try:
        with closing(sqlite3.connect(path.resolve().as_uri()+'?mode=ro', uri=True, timeout=.3)) as conn:
            conn.row_factory = sqlite3.Row
            conn.execute('PRAGMA query_only=ON')
            deadline = time.monotonic()+.5
            conn.set_progress_handler(lambda: int(time.monotonic()>deadline), 1000)
            reader = SimpleNamespace(query_one=lambda sql, params=(): conn.execute(sql, params).fetchone())
            snapshot = worker_state.evaluate(reader, worker,
                SimpleNamespace(heartbeat_stale_seconds=heartbeat_stale_seconds,
                                probe_interval_seconds=probe_interval_seconds), now=now)
            if not (snapshot.heartbeat_fresh and snapshot.probe_fresh and snapshot.instance_consistent):
                return data
            # A crashed process can leave historical unlimited-Key rows pending.
            # Only rows from the current Gateway lifetime/deadline are live.
            cutoff = stamp(now-timedelta(seconds=request_deadline_seconds))
            started = conn.execute("SELECT MAX(at) FROM audit_events WHERE category='service' AND action='started'").fetchone()[0]
            if started and started > cutoff:
                cutoff = started
            active = conn.execute('SELECT COUNT(*) FROM requests WHERE worker_id=? AND ended_at IS NULL AND started_at>=? AND started_at<=?',
                                  (worker, cutoff, stamp(now))).fetchone()[0]
            data.update(active_requests=active, available=True)
    except (sqlite3.Error, OSError, ValueError, TypeError):
        pass
    return data


def create_public_app(path: Path, worker: str, stale_seconds=300,
                      heartbeat_stale_seconds=35, probe_interval_seconds=15,
                      concurrency_limit=2, limits_path=None, request_deadline_seconds=1800):
    app = FastAPI(docs_url=None, redoc_url=None, openapi_url=None)
    cache = {}
    lock = threading.Lock()

    @app.get("/")
    def home():
        return FileResponse(Path(__file__).parent / "web/dist/public.html",
                            headers={"Cache-Control": "no-store"})

    @app.get("/public/api/overview")
    def overview(range: str = "today", live: bool = False):
        if range not in RANGES:
            raise HTTPException(400, "Unsupported range")
        processing = processing_snapshot(path, worker, concurrency_limit, limits_path=limits_path,
            request_deadline_seconds=request_deadline_seconds,
            heartbeat_stale_seconds=heartbeat_stale_seconds, probe_interval_seconds=probe_interval_seconds)
        if live:
            return JSONResponse({'processing': processing}, headers={'Cache-Control': 'no-store'})
        with lock:
            if range not in cache or time.monotonic() - cache[range][0] > 15:
                try:
                    data = aggregate(path, worker, range, stale_seconds=stale_seconds,
                                     heartbeat_stale_seconds=heartbeat_stale_seconds,
                                     probe_interval_seconds=probe_interval_seconds)
                except (sqlite3.Error, OSError, ValueError, TypeError):
                    raise HTTPException(503, "Summary temporarily unavailable") from None
                cache[range] = (time.monotonic(), data)
            data = cache[range][1]
        return JSONResponse(dict(data, processing=processing), headers={"Cache-Control": "no-store"})

    @app.get("/healthz")
    def health():
        return {"status": "ok"}

    return app


def main():
    import argparse
    import uvicorn
    from .config import load_config
    parser = argparse.ArgumentParser()
    parser.add_argument("--config", required=True)
    parser.add_argument("--port", type=int, default=8081)
    args = parser.parse_args()
    config = load_config(args.config)
    uvicorn.run(create_public_app(config.db_file, config.worker_id, config.quota_stale_seconds,
                                 config.heartbeat_stale_seconds, config.probe_interval_seconds,
                                 config.max_concurrency,
                                 Path('/opt/codex-relay-worker/runtime-limits.json'), config.request_deadline_seconds),
                host="127.0.0.1", port=args.port, access_log=False)


if __name__ == "__main__":
    main()
