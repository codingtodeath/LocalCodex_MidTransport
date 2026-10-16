"""管理员接口。

见 01_DEVELOPMENT_SPEC.md 第 12 节。要点：
* 仅服务端会话 Cookie；Cookie 不能代替模型 Key，Key 也不能登录后台。
* 状态变更校验 CSRF Token 与 Origin；禁止宽松 CORS。
* 请求流水只返回元数据：无 prompt、源码、模型正文或工具参数。
* CSV 导出防公式注入。
* 无凭证下载、远程执行、任意请求代理和充值按钮。
"""

from __future__ import annotations

import csv
import io
import sqlite3
from datetime import UTC, datetime, timedelta
from typing import Any

from fastapi import APIRouter, Request, Response
from fastapi.responses import JSONResponse, StreamingResponse

from . import auth as auth_mod
from . import commands as cmd
from . import pricing as pricing_mod
from . import quota_store, worker_state
from .db import audit, parse_iso, utc_now
from .errors import RelayError, csrf_failed, invalid_request, not_authenticated
from .models import CommandResult
from .services import Services

router = APIRouter(prefix="/admin/api")

SESSION_COOKIE = "relay_session"
CSRF_HEADER = "x-relay-csrf"

# 允许的浏览器来源由实际部署域名决定；空表示“同源即可”。
SAFE_METHODS = ("GET", "HEAD", "OPTIONS")


def _client_ip(request: Request) -> str:
    services: Services = request.app.state.services
    forwarded = request.headers.get("x-forwarded-for")
    peer = request.client.host if request.client else ""
    # 只有在 loopback 代理可信时才采信 X-Forwarded-For。
    if forwarded and peer in set(services.config.trusted_proxy_hosts):
        return forwarded.split(",")[0].strip()[:64]
    return peer[:64]


def _services(request: Request) -> Services:
    return request.app.state.services


def _cookie_secure(services: Services) -> bool:
    return services.config.is_production or services.config.public_base_url.startswith("https://")


def _allowed_origins(request: Request, services: Services) -> list[str]:
    """允许的浏览器 Origin。

    优先使用配置的 public_base_url；未配置时退化为“请求自身的 host”，
    这样即使开发环境也绝不放行跨站 Origin。
    """

    allowed: list[str] = []
    configured = services.config.public_base_url.rstrip("/")
    if configured:
        allowed.append(configured)
    host = request.headers.get("host")
    if host:
        allowed.append(f"{request.url.scheme}://{host}")
    return allowed


def _origin_ok(request: Request, services: Services) -> bool:
    origin = request.headers.get("origin")
    if origin:
        normalised = origin.rstrip("/")
        if any(normalised == candidate for candidate in _allowed_origins(request, services)):
            return True
        return False
    # 浏览器跨站请求会带 Origin；没有 Origin 时看 Sec-Fetch-Site。
    if request.headers.get("sec-fetch-site") == "cross-site":
        return False
    return True


def require_session(request: Request) -> auth_mod.AdminSession:
    services = _services(request)
    token = request.cookies.get(SESSION_COOKIE)
    if not token:
        raise not_authenticated()
    session = auth_mod.load_session(services.db, services.session_secret, token)
    if session is None:
        raise not_authenticated()
    if request.method not in SAFE_METHODS:
        # 双重提交：Cookie 之外还必须带正确的 CSRF Token。
        provided = request.headers.get(CSRF_HEADER)
        if not auth_mod.csrf_valid(session, services.session_secret, provided):
            raise csrf_failed()
        # 禁止宽松 CORS：Origin 必须与本站一致。
        if not _origin_ok(request, services):
            raise csrf_failed()
    return session


# --- 登录 / 退出 --------------------------------------------------------


@router.post("/login")
async def login(request: Request) -> Response:
    services = _services(request)
    config = services.config
    ip = _client_ip(request)

    try:
        payload = await request.json()
    except Exception as exc:
        raise invalid_request("Login body must be JSON.") from exc
    if not isinstance(payload, dict):
        raise invalid_request("Login body must be a JSON object.")

    username = payload.get("username")
    password = payload.get("password")
    if not isinstance(username, str) or not isinstance(password, str):
        raise invalid_request("username and password are required.")

    if auth_mod.login_rate_limited(services.db, ip, config.login_rate_limit_per_minute):
        audit(services.db, "admin", "login_rate_limited", actor=ip)
        # 统一错误，不区分“密码错”和“被限速”。
        raise RelayError(429, "login_failed", "Login failed.", error_type="invalid_request_error", retry_after=60)

    ok = auth_mod.verify_admin(services.db, username, password)
    auth_mod.record_login_attempt(services.db, ip, ok)
    if not ok:
        audit(services.db, "admin", "login_failed", actor=ip, detail={"username_len": len(username)})
        # 统一错误消息，避免用户名枚举。
        raise RelayError(
            401, "login_failed", "Invalid credentials.", error_type="invalid_request_error"
        )

    # 成功即轮换会话标识，防会话固定。
    token, session = auth_mod.create_session(
        services.db,
        services.session_secret,
        username,
        config.admin_session_seconds,
        user_agent=request.headers.get("user-agent"),
    )
    audit(services.db, "admin", "login_success", actor=username, detail={"ip": ip})

    response = JSONResponse(
        content={
            "ok": True,
            "username": username,
            "csrf_token": auth_mod.csrf_token_for(session, services.session_secret),
            "expires_at": session.expires_at,
        }
    )
    response.set_cookie(
        SESSION_COOKIE,
        token,
        httponly=True,
        secure=_cookie_secure(services),
        samesite="strict",
        path="/admin",
        max_age=config.admin_session_seconds,
    )
    return response


@router.post("/logout")
async def logout(request: Request) -> Response:
    session = require_session(request)
    services = _services(request)
    token = request.cookies.get(SESSION_COOKIE) or ""
    auth_mod.revoke_session(services.db, services.session_secret, token)
    audit(services.db, "admin", "logout")
    response = JSONResponse(content={"ok": True})
    response.delete_cookie(SESSION_COOKIE, path="/admin")
    return response


@router.get("/session")
async def session_info(request: Request) -> Response:
    session = require_session(request)
    services = _services(request)
    return JSONResponse(
        content={
            "authenticated": True,
            "csrf_token": auth_mod.csrf_token_for(session, services.session_secret),
        }
    )


# --- 概览 ---------------------------------------------------------------


def _band(value: Any, thresholds: tuple[float, float], labels: tuple[str, str, str]) -> str:
    if value is None:
        return labels[2]
    if value < thresholds[0]:
        return labels[0]
    if value < thresholds[1]:
        return labels[1]
    return labels[2]


@router.get("/overview")
async def overview(request: Request) -> Response:
    require_session(request)
    services = _services(request)
    db = services.db
    config = services.config

    snapshot = worker_state.evaluate(db, config.worker_id, config)
    quota_view = quota_store.build_view(
        db, config.worker_id, config, worker_online=snapshot.state == worker_state.STATE_ONLINE
    )

    day = datetime.now(UTC).strftime("%Y-%m-%d")
    totals = db.query_one(
        "SELECT COUNT(*) AS requests, "
        "COALESCE(SUM(CASE WHEN usage_status = 'reported' THEN 1 ELSE 0 END),0) AS metered, "
        "COALESCE(SUM(CASE WHEN usage_status IN ('missing','invalid','partial') THEN 1 ELSE 0 END),0) AS unknown_usage, "
        "COALESCE(SUM(CASE WHEN estimate_status = 'ok' THEN 1 ELSE 0 END),0) AS estimable, "
        "COALESCE(SUM(input_tokens),0) AS input_tokens, "
        "COALESCE(SUM(cached_input_tokens),0) AS cached_input_tokens, "
        "COALESCE(SUM(output_tokens),0) AS output_tokens, "
        "COALESCE(SUM(reasoning_tokens),0) AS reasoning_tokens, "
        "COALESCE(SUM(total_tokens),0) AS total_tokens "
        "FROM requests WHERE DATE(started_at) = ?",
        (day,),
    )
    rollup = db.query_one(
        "SELECT COALESCE(SUM(estimated_cost),'0') AS cost, COUNT(*) AS models FROM daily_rollups WHERE day = ?",
        (day,),
    )
    # 逐条 Decimal 累加，避免浮点误差。
    from decimal import Decimal, InvalidOperation

    cost_rows = db.query(
        "SELECT estimated_cost FROM requests WHERE DATE(started_at) = ? AND estimated_cost IS NOT NULL",
        (day,),
    )
    cost = Decimal(0)
    for row in cost_rows:
        try:
            cost += Decimal(row["estimated_cost"])
        except (InvalidOperation, TypeError):
            continue

    requests_total = int(totals["requests"]) if totals else 0
    metered = int(totals["metered"]) if totals else 0
    estimable = int(totals["estimable"]) if totals else 0

    last_success = db.query_one(
        "SELECT ended_at FROM requests WHERE response_status = 'completed' "
        "ORDER BY ended_at DESC LIMIT 1"
    )

    return JSONResponse(
        content={
            "server_time": utc_now(),
            "worker": snapshot.as_dict(),
            "quota": quota_view.as_dict(),
            "counters": {
                "active_requests": services.gate.global_active if services.gate else 0,
                "last_inference_success_at": (
                    last_success["ended_at"] if last_success else snapshot.last_inference_success_at
                ),
            },
            "today": {
                "day": day,
                "timezone": "UTC",
                "request_count": requests_total,
                "reported_usage_count": metered,
                "unknown_usage_count": int(totals["unknown_usage"]) if totals else 0,
                "estimable_count": estimable,
                "usage_coverage": round(metered / requests_total, 4) if requests_total else None,
                "estimate_coverage": round(estimable / requests_total, 4) if requests_total else None,
                "input_tokens": int(totals["input_tokens"]) if totals else 0,
                "cached_input_tokens": int(totals["cached_input_tokens"]) if totals else 0,
                "output_tokens": int(totals["output_tokens"]) if totals else 0,
                "reasoning_tokens": int(totals["reasoning_tokens"]) if totals else 0,
                "total_tokens": int(totals["total_tokens"]) if totals else 0,
                "api_equivalent_cost": str(cost.quantize(Decimal("0.000001"))),
                "currency": "USD",
                "cost_label": "API 等价估算费用 · 非实际账单",
                "actual_billing": "not_integrated",
            },
            "concurrency": {
                "global_limit": config.max_concurrency,
                "per_client_limit": config.per_client_concurrency,
                "active": services.gate.snapshot() if services.gate else {},
            },
        }
    )


# --- 额度 ---------------------------------------------------------------


@router.get("/quota")
async def quota(request: Request) -> Response:
    require_session(request)
    services = _services(request)
    snapshot = worker_state.evaluate(services.db, services.config.worker_id, services.config)
    view = quota_store.build_view(
        services.db,
        services.config.worker_id,
        services.config,
        worker_online=snapshot.state == worker_state.STATE_ONLINE,
    )
    account_usage = quota_store.get_account_usage(services.db, services.config.worker_id)
    return JSONResponse(
        content={
            "worker_state": snapshot.state,
            "quota": view.as_dict(),
            "refreshable": snapshot.state == worker_state.STATE_ONLINE,
            "account_usage": account_usage,
            "account_usage_note": "账号级汇总为独立口径，不能与本网关 Token 流水相加。",
        }
    )


@router.post("/quota/refresh")
async def refresh_quota(request: Request) -> Response:
    require_session(request)
    services = _services(request)
    snapshot = worker_state.evaluate(services.db, services.config.worker_id, services.config)
    servable, _ = worker_state.is_servable(snapshot)
    decision = cmd.create_refresh_command(
        services.db,
        services.config.worker_id,
        services.config,
        worker_servable=snapshot.state == worker_state.STATE_ONLINE,
    )
    status_code = 202 if decision.created or decision.status in ("already_pending", "debounced") else 409
    return JSONResponse(
        status_code=status_code,
        content={
            "created": decision.created,
            "command_id": decision.command_id,
            "status": decision.status,
            "reason": decision.reason,
            "expires_at": decision.expires_at,
            "worker_servable": servable,
            "note": (
                "命令将在下一次心跳响应中下发；Windows 离线时不会立即获得实时数据。"
                if decision.created
                else "未创建新命令（去抖或已有待执行命令）。"
            ),
        },
    )


@router.get("/quota/commands/{command_id}")
async def command_status(request: Request, command_id: str) -> Response:
    require_session(request)
    services = _services(request)
    status = cmd.command_status(services.db, command_id)
    if status is None:
        raise RelayError(404, "not_found", "Unknown command.", error_type="invalid_request_error")
    return JSONResponse(content=status)


# --- 请求流水 -----------------------------------------------------------


@router.get("/requests")
async def list_requests(request: Request) -> Response:
    require_session(request)
    services = _services(request)

    params = request.query_params
    try:
        limit = min(max(int(params.get("limit", "50")), 1), 200)
        offset = max(int(params.get("offset", "0")), 0)
    except ValueError as exc:
        raise invalid_request("limit/offset must be integers.") from exc

    from .key_management import request_filter
    where, args = request_filter(params)
    rows = services.db.query(
        "SELECT request_id, client_key_id, COALESCE((SELECT alias FROM client_keys k WHERE k.key_id=requests.client_key_id),client_alias) AS client_alias, requested_model, response_model, stream, started_at, ended_at, "
        "duration_ms, first_byte_ms, http_status, response_status, error_code, "
        "input_tokens, cached_input_tokens, output_tokens, reasoning_tokens, total_tokens, "
        "usage_status, estimate_status, estimated_cost, currency, price_version, usage_conflict "
        f"FROM requests {where} ORDER BY started_at DESC LIMIT ? OFFSET ?",
        (*args, limit, offset),
    )
    total = services.db.query_one(f"SELECT COUNT(*) AS n FROM requests {where}", tuple(args))

    # 只返回元数据；不含 prompt、正文或工具参数。
    items = [
        {
            "request_id": row["request_id"],
            "client_key_id": row["client_key_id"],
            "client_alias": row["client_alias"],
            "requested_model": row["requested_model"],
            "response_model": row["response_model"],
            "stream": bool(row["stream"]),
            "started_at": row["started_at"],
            "ended_at": row["ended_at"],
            "duration_ms": row["duration_ms"],
            "first_byte_ms": row["first_byte_ms"],
            "http_status": row["http_status"],
            "response_status": row["response_status"],
            "error_code": row["error_code"],
            "input_tokens": row["input_tokens"],
            "cached_input_tokens": row["cached_input_tokens"],
            "output_tokens": row["output_tokens"],
            "reasoning_tokens": row["reasoning_tokens"],
            "total_tokens": row["total_tokens"],
            "usage_status": row["usage_status"],
            "estimate_status": row["estimate_status"],
            "estimated_cost": row["estimated_cost"],
            "currency": row["currency"],
            "price_version": row["price_version"],
            "usage_conflict": bool(row["usage_conflict"]),
        }
        for row in rows
    ]
    return JSONResponse(
        content={"items": items, "total": int(total["n"]) if total else 0, "limit": limit, "offset": offset}
    )


@router.get("/requests.csv")
async def export_requests(request: Request) -> Response:
    require_session(request)
    services = _services(request)
    from .key_management import request_filter
    where, args = request_filter(request.query_params)
    rows = services.db.query(
        "SELECT request_id, started_at, COALESCE((SELECT alias FROM client_keys k WHERE k.key_id=requests.client_key_id),client_alias) AS client_alias, COALESCE(response_model, requested_model) AS model, "
        "stream, http_status, response_status, error_code, input_tokens, cached_input_tokens, "
        "output_tokens, reasoning_tokens, total_tokens, usage_status, estimate_status, "
        "estimated_cost, currency FROM requests" + where + " ORDER BY started_at DESC LIMIT 10000", args
    )

    buffer = io.StringIO()
    writer = csv.writer(buffer)
    writer.writerow(
        [
            "request_id",
            "started_at",
            "client_alias",
            "model",
            "stream",
            "http_status",
            "response_status",
            "error_code",
            "input_tokens",
            "cached_input_tokens",
            "output_tokens",
            "reasoning_tokens",
            "total_tokens",
            "usage_status",
            "estimate_status",
            "estimated_cost",
            "currency",
        ]
    )
    for row in rows:
        writer.writerow([_csv_safe(v) for v in tuple(row)])

    payload = ("\ufeff" + buffer.getvalue()).encode("utf-8")
    return StreamingResponse(
        io.BytesIO(payload),
        media_type="text/csv; charset=utf-8",
        headers={"Content-Disposition": 'attachment; filename="relay-requests.csv"'},
    )


def _csv_safe(value: Any) -> Any:
    """CSV 公式注入防护：以 = + - @ 开头的内容强制加前缀。"""

    if not isinstance(value, str):
        return value
    if value[:1] in ("=", "+", "-", "@", "\t", "\r"):
        return "'" + value
    return value


# --- 客户端 Key ---------------------------------------------------------


@router.get("/client-keys")
async def list_client_keys(request: Request) -> Response:
    require_session(request)
    services = _services(request)
    from .key_management import list_keys
    return list_keys(services)


@router.post("/client-keys")
async def create_client_key(request: Request) -> Response:
    require_session(request)
    from .key_management import create_key
    return await create_key(request, _services(request))


@router.delete("/client-keys/{key_id}")
async def revoke_client_key(request: Request, key_id: str) -> Response:
    require_session(request)
    services = _services(request)
    changed = auth_mod.revoke_client_key(services.db, key_id)
    if not changed:
        raise RelayError(404, "not_found", "Unknown or already revoked key.", error_type="invalid_request_error")
    return JSONResponse(content={"ok": True, "key_id": key_id, "note": "已撤销；后续请求立即失败。"})


# --- 价格 ---------------------------------------------------------------


@router.get("/prices")
async def list_prices(request: Request) -> Response:
    require_session(request)
    services = _services(request)
    rows = services.db.query(
        "SELECT rule_id, price_version, model, service_tier, input_context_min, input_context_max, "
        "formula, cache_write_policy, price_input, price_cached_input, price_output, price_cache_write, "
        "currency, excluded_costs_json, source_url, verified_at, effective_from, created_by, created_at "
        "FROM price_rules ORDER BY effective_from DESC, rule_id DESC LIMIT 500"
    )
    import json as _json

    items = []
    for row in rows:
        item = dict(row)
        try:
            item["excluded_costs"] = _json.loads(item.pop("excluded_costs_json") or "[]")
        except _json.JSONDecodeError:
            item["excluded_costs"] = []
        items.append(item)
    return JSONResponse(content={"items": items, "count": len(items)})


@router.post("/prices")
async def add_price(request: Request) -> Response:
    require_session(request)
    services = _services(request)
    try:
        payload = await request.json()
    except Exception as exc:
        raise invalid_request("Body must be JSON.") from exc
    if not isinstance(payload, dict):
        raise invalid_request("Body must be a JSON object.")
    if payload.get("test_only"):
        raise invalid_request("生产环境拒绝 test_only 价格规则。")

    required = ("price_version", "model", "effective_from", "currency", "prices_per_million")
    missing = [name for name in required if name not in payload]
    if missing:
        raise invalid_request(f"缺少字段：{', '.join(missing)}")

    try:
        rule = pricing_mod.insert_price_rule(services.db, payload, created_by="admin")
    except pricing_mod.PriceRuleError as exc:
        raise invalid_request(str(exc)) from exc
    except sqlite3.IntegrityError as exc:
        # 规则不可变：同一版本重复插入是冲突，不是静默覆盖。
        raise invalid_request(
            "该 price_version + model + 上下文区间已存在；价格规则不可变，请使用新的版本号。"
        ) from exc
    except Exception as exc:
        raise invalid_request("无法写入价格规则。") from exc

    services.reload_prices()
    audit(
        services.db,
        "pricing",
        "rule_added",
        detail={"price_version": rule.price_version, "model": rule.model},
    )
    return JSONResponse(
        status_code=201,
        content={
            "ok": True,
            "price_version": rule.price_version,
            "model": rule.model,
            "effective_from": rule.effective_from,
            "note": "只影响生效时间之后的请求；历史记录保留原 price_version。",
        },
    )


@router.get("/prices/effective")
async def effective_prices(request: Request) -> Response:
    require_session(request)
    services = _services(request)
    return JSONResponse(
        content={
            "rules": len(services.price_book),
            "models": sorted({rule.model for rule in services.price_book.rules}),
            "note": "未知模型未配置价格时显示不可估价，不默认免费。",
        }
    )


# --- 账号级汇总（可选，独立口径） ---------------------------------------


@router.get("/account-usage")
async def account_usage(request: Request) -> Response:
    require_session(request)
    services = _services(request)
    data = quota_store.get_account_usage(services.db, services.config.worker_id)
    return JSONResponse(
        content={
            "account_usage": data,
            "note": "官方账号级汇总；与本网关 Token 流水口径不同，不能相加。",
        }
    )


from .key_management import router as key_router
router.include_router(key_router)
