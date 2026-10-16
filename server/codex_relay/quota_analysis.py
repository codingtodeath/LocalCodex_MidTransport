"""Authenticated, bounded, read-only quota/cost analysis sidecar."""
from __future__ import annotations

import json
import math
import sqlite3
import threading
import time
from datetime import UTC, datetime, timedelta
from decimal import Decimal, InvalidOperation
from pathlib import Path
from types import SimpleNamespace

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import JSONResponse

from .auth import read_secret_file, session_hash
from .db import parse_iso
from .pricing import PriceBook, load_rules_for_db

MAX_SNAPSHOTS = 50000
MAX_REQUESTS = 20000
MAX_GAP_SECONDS = 900


def stamp(value):
    return value.astimezone(UTC).isoformat(timespec="milliseconds").replace("+00:00", "Z")


def interval(start, end, now):
    try:
        a, b = parse_iso(start), parse_iso(end)
        if not a or not b or not a.tzinfo or not b.tzinfo:
            raise ValueError()
        if not any(c in start[10:] for c in ("Z", "+", "-")) or not any(c in end[10:] for c in ("Z", "+", "-")):
            raise ValueError()
        if not a < b or b - a > timedelta(days=31) or b > now + timedelta(seconds=60):
            raise ValueError()
        return a, min(b, now)
    except (ValueError, TypeError, AttributeError):
        raise HTTPException(400, "请选择带时区的有效起止时间，区间最多 31 天，结束时间不能在未来。") from None


def connection(path):
    c = sqlite3.connect(path.resolve().as_uri() + "?mode=ro", uri=True, timeout=1)
    c.row_factory = sqlite3.Row
    c.execute("PRAGMA query_only=ON")
    deadline = time.monotonic() + 3
    c.set_progress_handler(lambda: int(time.monotonic() > deadline), 1000)
    return c


def cost_bounds(row, book):
    """Partition input into uncached, cache-read and cache-write; never double bill."""
    if row["usage_status"] != "reported" or row["usage_conflict"]:
        return None, "usage_missing_or_conflicted"
    i, h, o, w = (row[k] for k in ("input_tokens", "cached_input_tokens", "output_tokens", "cache_write_tokens"))
    if any(type(v) is not int or v < 0 for v in (i, h, o)) or h > i:
        return None, "invalid_or_missing_tokens"
    rule = book.select(row["response_model"] or row["requested_model"], input_tokens=i,
                       service_tier="standard", at=row["started_at"])
    if rule is None or rule.formula != "input_cached_output_v1":
        return None, "price_not_available"
    rates = [rule.price_input, rule.price_cached_input, rule.price_output]
    if any(v is None or not v.is_finite() or v < 0 for v in rates):
        return None, "price_not_available"
    u = i - h
    fixed = Decimal(h) * rule.price_cached_input + Decimal(o) * rule.price_output
    uncertain = False
    if rule.cache_write_policy == "separate":
        write_rate = rule.price_cache_write
        if write_rate is None or not write_rate.is_finite() or write_rate < 0:
            return None, "price_not_available"
        if w is None:
            # Bound unknown write volume between zero and all uncached input.
            low, high = sorted((Decimal(u) * rule.price_input, Decimal(u) * write_rate))
            uncertain = u > 0 and low != high
        elif type(w) is not int or not 0 <= w <= u:
            return None, "invalid_cache_write_tokens"
        else:
            low = high = Decimal(u - w) * rule.price_input + Decimal(w) * write_rate
    else:
        low = high = Decimal(u) * rule.price_input
    return (fixed / 1000000 + low / 1000000, fixed / 1000000 + high / 1000000,
            rule.currency, rule.price_version, uncertain), None


def money(value):
    return str(value.quantize(Decimal("0.000001"))) if value is not None else None


def analyze(c, worker, start, end, confirmed, *, now=None, client_key_id=None):
    now = now or datetime.now(UTC)
    requested_start, requested_end = interval(start, end, now)
    snapshots = c.execute("""SELECT captured_at, account_epoch, identity_check, windows_json
        FROM quota_snapshots WHERE worker_id=? AND status='ok' AND source='codex_app_server'
        AND captured_at>=? AND captured_at<=? ORDER BY captured_at, snapshot_id LIMIT ?""",
        (worker, stamp(requested_start), stamp(requested_end), MAX_SNAPSHOTS + 1)).fetchall()
    if len(snapshots) > MAX_SNAPSHOTS:
        raise HTTPException(422, "快照数量过多，请缩短区间。")
    result = {"requested_start": stamp(requested_start), "requested_end": stamp(requested_end),
              "timezone": "Asia/Hong_Kong", "effective_start": None, "effective_end": None,
              "snapshot_count": len(snapshots), "external_usage_excluded_by_user": confirmed,
              "windows": [], "cost": None, "models": [], "issues": [],
              "price_basis": "请求发生时已生效的 Standard 模型 Token 单价；本页重新计算，不修改原流水。",
              "client_key_id": client_key_id,
              "note": "账号百分比包含中继外使用；比例仅代表所选样本，不是订阅额度的固定兑换率或实际账单。"}
    if len(snapshots) < 2:
        result["issues"] = ["not_enough_snapshots"]
        return result
    a, b = parse_iso(snapshots[0]["captured_at"]), parse_iso(snapshots[-1]["captured_at"])
    if a >= b:
        result["issues"] = ["not_enough_snapshots"]
        return result
    result.update(effective_start=stamp(a), effective_end=stamp(b),
                  start_trim_seconds=round((a - requested_start).total_seconds()),
                  end_trim_seconds=round((requested_end - b).total_seconds()))
    epochs = {s["account_epoch"] for s in snapshots}
    if len(epochs) != 1 or None in epochs or "" in epochs:
        result["issues"].append("account_changed_or_unknown")
    if any(s["identity_check"] not in ("automatic_match", "manual_confirmed") for s in snapshots):
        result["issues"].append("identity_unverified")
    if client_key_id:
        result["issues"].append("key_quota_not_attributable")
    if not confirmed:
        result["issues"].append("external_usage_not_confirmed")

    reader = SimpleNamespace(query=lambda sql, params=(): list(c.execute(sql, params)))
    rules = [r for r in load_rules_for_db(reader, at=stamp(b))
             if r.service_tier in (None, "standard") and r.modality == "text"]
    book = PriceBook(rules)
    requests = c.execute("""SELECT started_at,ended_at,requested_model,response_model,usage_status,
        usage_conflict,input_tokens,cached_input_tokens,cache_write_tokens,output_tokens
        FROM requests WHERE worker_id=? AND started_at<? AND (ended_at>? OR ended_at IS NULL)
        AND (? IS NULL OR client_key_id=?)
        ORDER BY started_at LIMIT ?""", (worker, stamp(b), stamp(a), client_key_id, client_key_id, MAX_REQUESTS + 1)).fetchall()
    if len(requests) > MAX_REQUESTS:
        raise HTTPException(422, "请求数量过多，请缩短区间。")
    contained = [r for r in requests if r["ended_at"] and parse_iso(r["started_at"]) >= a
                 and parse_iso(r["ended_at"]) <= b]
    crossing = len(requests) - len(contained)
    groups = {}
    failures = {}
    currencies, versions = set(), set()
    priced = uncertain = 0
    lo = hi = Decimal(0)
    tokens = {k: 0 for k in ("input_tokens", "cached_input_tokens", "output_tokens")}
    for r in contained:
        model = r["response_model"] or r["requested_model"] or "unknown"
        group = groups.setdefault(model, {"model": model, "requests": 0, "priced_requests": 0,
                    "input_tokens": 0, "cached_input_tokens": 0, "output_tokens": 0,
                    "cost_min": Decimal(0), "cost_max": Decimal(0), "currencies": set()})
        group["requests"] += 1
        if r["usage_status"] == "reported" and not r["usage_conflict"]:
            for k in tokens:
                if type(r[k]) is int and r[k] >= 0:
                    tokens[k] += r[k]
                    group[k] += r[k]
        bound, issue = cost_bounds(r, book)
        if issue:
            failures[issue] = failures.get(issue, 0) + 1
            continue
        low, high, currency, version, is_uncertain = bound
        priced += 1; uncertain += int(is_uncertain)
        lo += low; hi += high; currencies.add(currency); versions.add(version)
        group["priced_requests"] += 1
        group["cost_min"] += low; group["cost_max"] += high; group["currencies"].add(currency)
    if crossing:
        result["issues"].append("requests_cross_boundary")
    if not contained:
        result["issues"].append("no_contained_requests")
    if priced < len(contained):
        result["issues"].append("incomplete_cost_coverage")
    if len(currencies) > 1:
        result["issues"].append("mixed_currencies")
    cost = dict(requests=len(contained), crossing_requests=crossing, priced_requests=priced,
                unpriced_requests=len(contained)-priced, uncertain_write_requests=uncertain,
                coverage=priced/len(contained) if contained else None,
                currency=next(iter(currencies)) if len(currencies) == 1 else None,
                min=money(lo) if priced and len(currencies) == 1 else None,
                max=money(hi) if priced and len(currencies) == 1 else None,
                price_versions=sorted(versions), missing_reasons=failures, **tokens)
    cost["total_tokens"] = tokens["input_tokens"] + tokens["output_tokens"]
    result["cost"] = cost
    for g in groups.values():
        cs = g.pop("currencies")
        g["currency"] = next(iter(cs)) if len(cs) == 1 else None
        for k in ("cost_min", "cost_max"):
            g[k] = money(g[k]) if g["priced_requests"] and len(cs) == 1 else None
    result["models"] = sorted(groups.values(), key=lambda g: g["model"])

    parsed = [json.loads(s["windows_json"]) for s in snapshots]
    keys = sorted({(w["bucket_id"], w["window_id"]) for ws in parsed for w in ws})
    for bucket, window in keys:
        series = [next((w for w in ws if (w["bucket_id"], w["window_id"]) == (bucket, window)), None)
                  for ws in parsed]
        valid = [w for w in series if w]
        item = valid[-1]
        points = Decimal(0)
        coverage = 0
        gaps = resets = missing = 0
        for index in range(1, len(series)):
            old, current = series[index-1:index+1]
            seconds = (parse_iso(snapshots[index]["captured_at"]) - parse_iso(snapshots[index-1]["captured_at"])).total_seconds()
            if old is None or current is None:
                missing += 1; continue
            before, after = Decimal(str(old["used_percent"])), Decimal(str(current["used_percent"]))
            old_reset, reset = parse_iso(old.get("resets_at")), parse_iso(current.get("resets_at"))
            if (not before.is_finite() or not after.is_finite() or before < 0 or after < 0
                    or before > 100 or after > 100):
                missing += 1; continue
            if (snapshots[index]["account_epoch"] != snapshots[index-1]["account_epoch"]
                    or after < before or not old_reset or not reset
                    or abs((reset-old_reset).total_seconds()) > 5
                    or old.get("window_minutes") != current.get("window_minutes")
                    or old_reset <= parse_iso(snapshots[index]["captured_at"])):
                resets += 1; continue
            if not 0 < seconds <= MAX_GAP_SECONDS:
                gaps += 1; continue
            points += after-before
            coverage += seconds
        issues = list(result["issues"])
        if gaps: issues.append("snapshot_gap")
        if resets: issues.append("reset_or_cycle_changed")
        if missing: issues.append("window_missing_or_invalid")
        if points <= 0: issues.append("no_observed_quota_increase")
        eligible = not issues
        result["windows"].append({"bucket_id": bucket, "window_id": window,
            "label": item.get("display_name") or (f'{item["window_minutes"]/1440:g} 天窗口' if
                        item.get("window_minutes", 0) and item["window_minutes"] >= 1440 else
                        f'{item.get("window_minutes") or "未知"} 分钟窗口'),
            "start_used_percent": series[0]["used_percent"] if series[0] else None,
            "end_used_percent": series[-1]["used_percent"] if series[-1] else None,
            "observed_used_points": float(points), "coverage_seconds": coverage,
            "duration_seconds": (b-a).total_seconds(), "gap_count": gaps, "reset_count": resets,
            "ratio_available": eligible, "issues": issues,
            "cost_per_point_min": money(lo/points) if eligible else None,
            "cost_per_point_max": money(hi/points) if eligible else None})
    return result


def create_app(path: Path, worker: str, secret: bytes):
    app = FastAPI(docs_url=None, redoc_url=None, openapi_url=None)
    slots = threading.BoundedSemaphore(2)

    @app.exception_handler(HTTPException)
    async def handled_error(request, exc):
        return JSONResponse({"error": {"message": exc.detail}}, status_code=exc.status_code,
                            headers={"Cache-Control": "no-store"})

    @app.get("/admin/api/quota-analysis")
    def analysis(request: Request, start: str = "", end: str = "", external_usage_excluded: bool = False, client_key_id: str = ""):
        if not slots.acquire(blocking=False):
            raise HTTPException(503, "分析繁忙，请稍后重试。")
        c = None
        try:
            c = connection(path)
            token = request.cookies.get("relay_session", "")
            if not token or len(token) > 256:
                raise HTTPException(401, "请先登录管理员账号。")
            row = c.execute("SELECT expires_at,revoked_at FROM admin_sessions WHERE session_hash=?",
                            (session_hash(token, secret),)).fetchone()
            if not row or row["revoked_at"] or not parse_iso(row["expires_at"]) or parse_iso(row["expires_at"]) <= datetime.now(UTC):
                raise HTTPException(401, "请先登录管理员账号。")
            c.execute("BEGIN")  # One consistent read snapshot for quota, prices and requests.
            if client_key_id and not c.execute("SELECT 1 FROM client_keys WHERE key_id=?", (client_key_id,)).fetchone():
                raise HTTPException(400, "未知 Key。")
            data = analyze(c, worker, start, end, external_usage_excluded, client_key_id=client_key_id or None)
            return JSONResponse(data, headers={"Cache-Control": "no-store"})
        except HTTPException:
            raise
        except (sqlite3.Error, OSError, ValueError, TypeError, KeyError, InvalidOperation):
            raise HTTPException(503, "分析暂时不可用，请缩短区间或稍后重试。") from None
        finally:
            if c is not None: c.close()
            slots.release()

    @app.get("/healthz")
    def health():
        return {"status": "ok"}

    return app


def main():
    import argparse
    import uvicorn
    from .config import load_config
    p = argparse.ArgumentParser()
    p.add_argument("--config", required=True)
    p.add_argument("--port", type=int, default=8082)
    args = p.parse_args()
    config = load_config(args.config)
    app = create_app(config.db_file, config.worker_id, read_secret_file(config.session_secret_file))
    uvicorn.run(app, host="127.0.0.1", port=args.port, access_log=False)


if __name__ == "__main__":
    main()
