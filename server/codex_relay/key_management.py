"""Named client wallets and authenticated, key-specific diagnostic views."""
from __future__ import annotations

import json
from datetime import UTC, datetime
from decimal import Decimal

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse

from . import auth, key_wallet
from .db import audit, parse_iso
from .errors import RelayError, invalid_request

router = APIRouter()


def services_for(request):
    from .admin_api import require_session
    require_session(request)
    return request.app.state.services


async def payload_for(request):
    try:
        payload = await request.json()
    except (ValueError, UnicodeError):
        raise invalid_request("Body must be JSON.") from None
    if not isinstance(payload, dict):
        raise invalid_request("Body must be a JSON object.")
    return payload


def alias_value(value):
    if not isinstance(value, str) or not value.strip() or len(value.strip()) > 64:
        raise invalid_request("Name must contain 1 to 64 characters.")
    if any(ord(c) < 32 for c in value):
        raise invalid_request("Name must not contain control characters.")
    return value.strip()


def unique_alias(conn, alias, key_id=""):
    if any(r["alias"].casefold() == alias.casefold() for r in conn.execute(
            "SELECT alias FROM client_keys WHERE key_id<>? AND revoked_at IS NULL", (key_id,))):
        raise invalid_request("An active key already uses this name.")


def list_keys(services):
    # Never select or return the HMAC digest, let alone a plaintext credential.
    rows = services.db.query("SELECT key_id,alias,created_at,revoked_at,last_used_at,"
                             "balance_usd,balance_blocked,balance_updated_at FROM client_keys ORDER BY created_at DESC")
    return JSONResponse({"items": [dict(r, **key_wallet.view(r)) for r in rows]})


async def create_key(request, services):
    payload = await payload_for(request)
    alias = alias_value(payload.get("alias"))
    balance = key_wallet.balance_value(payload.get("balance_usd"))
    with services.db.write() as conn:
        unique_alias(conn, alias)
        token, key = auth.create_client_key(services.db, services.pepper, alias)
        key_wallet.set_balance(conn, key.key_id, balance)
    return JSONResponse({"key_id": key.key_id, "alias": key.alias, "created_at": key.created_at,
                         "balance_usd": balance, "currency": "USD", "plaintext_key": token,
                         "warning": "Key 只在创建时显示一次，请立即保存。"}, status_code=201)


@router.patch("/client-keys/{key_id}")
async def edit_key(request: Request, key_id: str):
    services = services_for(request)
    payload = await payload_for(request)
    if not payload or set(payload) - {"alias", "balance_usd"}:
        raise invalid_request("Only alias and balance_usd can be changed.")
    alias = alias_value(payload["alias"]) if "alias" in payload else None
    balance = key_wallet.balance_value(payload["balance_usd"]) if "balance_usd" in payload else None
    with services.db.write() as conn:
        row = conn.execute("SELECT revoked_at FROM client_keys WHERE key_id=?", (key_id,)).fetchone()
        if row is None:
            raise RelayError(404, "not_found", "Unknown API key.")
        if row["revoked_at"]:
            raise invalid_request("A revoked key cannot be edited.")
        if alias is not None:
            unique_alias(conn, alias, key_id)
            conn.execute("UPDATE client_keys SET alias=? WHERE key_id=?", (alias, key_id))
        if "balance_usd" in payload:
            key_wallet.set_balance(conn, key_id, balance)
        audit(services.db, "client_key", "updated", detail={"key_id": key_id, "fields": sorted(payload)})
    return JSONResponse({"ok": True, "key_id": key_id})


def request_filter(params):
    clauses, values = [], []
    for field, expression in (("day", "DATE(started_at,'+8 hours')"), ("client_key_id", "client_key_id"),
                              ("model", "COALESCE(response_model,requested_model)"), ("status", "response_status")):
        if params.get(field):
            clauses.append(expression + "=?")
            values.append(params[field])
    return " WHERE " + " AND ".join(clauses) if clauses else "", tuple(values)


def known_key(services, key_id):
    if not key_id:
        return None
    row = services.db.query_one("SELECT key_id,alias,balance_usd,balance_blocked,balance_updated_at,revoked_at "
                                "FROM client_keys WHERE key_id=?", (key_id,))
    if row is None:
        raise invalid_request("Unknown API key filter.")
    return row


@router.get("/key-usage")
def key_usage(request: Request):
    services = services_for(request)
    params = request.query_params
    key_id = params.get("client_key_id", "")
    key = known_key(services, key_id)
    where, args = request_filter({"client_key_id": key_id})
    clauses = [where.removeprefix(" WHERE ")] if where else []
    values = list(args)
    for field, operator in (("start", ">="), ("end", "<")):
        if params.get(field):
            raw = params[field]
            stamp = parse_iso(raw)
            if not stamp or not any(c in raw[10:] for c in ("Z", "+", "-")):
                raise invalid_request("Usage interval requires ISO dates with timezone.")
            clauses.append("started_at" + operator + "?")
            values.append(stamp.isoformat(timespec="milliseconds").replace("+00:00", "Z"))
    if params.get("start") and params.get("end") and parse_iso(params["start"]) >= parse_iso(params["end"]):
        raise invalid_request("Start must precede end.")
    where = " WHERE " + " AND ".join(clauses) if clauses else ""
    totals = services.db.query_one("SELECT COUNT(*) AS requests,COALESCE(SUM(total_tokens),0) AS total_tokens,"
        "COALESCE(SUM(input_tokens),0) AS input_tokens,COALESCE(SUM(cached_input_tokens),0) AS cached_input_tokens,"
        "COALESCE(SUM(output_tokens),0) AS output_tokens,"
        "COALESCE(SUM(CASE WHEN estimate_status='ok' AND currency='USD' THEN 1 ELSE 0 END),0) AS priced_requests "
        "FROM requests" + where, tuple(values))
    # Exact settlement values for new requests; historical display costs retain their original basis.
    rows = services.db.query("SELECT COALESCE((SELECT amount_usd FROM key_balance_entries e "
        "WHERE e.request_id=requests.request_id AND kind='charge'),estimated_cost) AS cost "
        "FROM requests" + where + (" AND " if where else " WHERE ") + "estimate_status='ok' AND currency='USD'",
        tuple(values))
    cost = sum((Decimal(r["cost"]) for r in rows if r["cost"] is not None), Decimal(0))
    data = dict(totals)
    data.update(cost_usd=str(cost.quantize(Decimal(".000001"))), currency="USD", client_key_id=key_id or None,
                unpriced_requests=data["requests"]-data["priced_requests"],
                wallet=dict(key, **key_wallet.view(key)) if key else None,
                note="费用按请求发生时的模型 Token 价格统计；订阅百分比属于整个账号，不能拆分到 Key。")
    return JSONResponse(data, headers={"Cache-Control": "no-store"})


@router.get("/connection-events")
def connection_events(request: Request):
    services = services_for(request)
    key_id = request.query_params.get("client_key_id", "")
    known_key(services, key_id)
    try:
        limit = min(max(int(request.query_params.get("limit", "100")), 1), 200)
    except ValueError:
        raise invalid_request("limit must be an integer.") from None
    names = {services.pepper.digest("diagnostic-client\0"+r["key_id"])[:32]: dict(r) for r in
             services.db.query("SELECT key_id,alias FROM client_keys")}
    fp = services.pepper.digest("diagnostic-client\0"+key_id)[:32] if key_id else None
    path = services.connection_log.path
    records = []
    # Bounded by logger rotation: 4 files of 2 MiB. Associate early events by request ID.
    for source in [path.with_name(f"gateway-events.{i}.jsonl") for i in (3, 2, 1)] + [path]:
        try:
            with source.open("rb") as stream:
                raw = stream.read(2*1024*1024 + 1024)
            for line in raw.splitlines():
                try:
                    r = json.loads(line)
                    if isinstance(r, dict):
                        records.append(r)
                except (ValueError, UnicodeError):
                    continue
        except OSError:
            continue
    identities = {r["request_id"]: r["client_fp"] for r in records if r.get("request_id") and r.get("client_fp")}
    result = []
    for r in reversed(records):
        fingerprint = r.get("client_fp") or identities.get(r.get("request_id"))
        if fp and fingerprint != fp:
            continue
        if r.get("event") not in {"request_finished", "request_rejected", "request_failed", "stream_finished"}:
            continue
        # Return only the logger's documented metadata, even if a file is manually modified.
        from .connection_log import NUMBERS, FLAGS
        safe = {k: v for k, v in r.items() if k in NUMBERS | FLAGS | {
            "at", "event", "request_id", "error_code", "outcome", "method", "path", "worker_state", "auth_state"}}
        identity = names.get(fingerprint, {})
        safe.update(client_key_id=identity.get("key_id"), client_alias=identity.get("alias"))
        result.append(safe)
        if len(result) >= limit:
            break
    return JSONResponse({"items": result, "limit": limit, "note": "近期连接元数据；鉴权前失败只能在全部 Key 下查看。"},
                        headers={"Cache-Control": "no-store"})
