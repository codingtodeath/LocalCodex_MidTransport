"""网关转发：客户端 -> Gateway -> 受限隧道 -> Windows Bridge。

见 01_DEVELOPMENT_SPEC.md 6.1 / 6.3 / 6.4 与第 10 节。原则：
* 只接受客户端有界 JSON；第一版明确拒绝压缩请求体。
* 客户端自带的追踪头必须被覆盖，防止伪造他人流水。
* 移除客户端 Authorization、Cookie、Host、Forwarded、X-Forwarded-*、
  Connection 及其列出的逐跳头，再换成 wkr_...。
* 不跟随上游重定向；未知 3xx 显式失败，避免认证头被发往其他主机。
* 流式读取保持到下游结束；计量旁路解析，不修改已传输字节。
* 数据库写失败不能静默：新请求返回 503 metering_unavailable。
"""

from __future__ import annotations

import json
import asyncio
import anyio
import time
import uuid
from typing import Any, AsyncIterator
from contextlib import asynccontextmanager

import httpx
from fastapi import Request
from starlette.background import BackgroundTask
from fastapi.responses import JSONResponse, Response, StreamingResponse

from . import errors as err
from . import pricing as pricing_mod
from . import stream_meter, worker_state
from .db import Database, audit, utc_now
from .services import Services
from . import contracts
from .connection_log import context, error_kind
from .key_wallet import KeyStopped

# 请求级逐跳头：绝不向上游转发。
HOP_BY_HOP = {
    "connection",
    "keep-alive",
    "proxy-authenticate",
    "proxy-authorization",
    "te",
    "trailer",
    "transfer-encoding",
    "upgrade",
}

# 客户端不可控的入站头：一律由网关重建。
STRIPPED_REQUEST_HEADERS = {
    "authorization",
    "cookie",
    "host",
    "forwarded",
    "x-forwarded-for",
    "x-forwarded-host",
    "x-forwarded-proto",
    "x-forwarded-port",
    "x-real-ip",
    "content-length",
    "accept-encoding",
    "x-request-id",
    "x-relay-request-id",
    "x-relay-edge-id",
    "openai-organization",
    "openai-project",
    "openai-beta",
}

# 只向下游转发安全的响应头。
SAFE_RESPONSE_HEADERS = {
    "content-type",
    "retry-after",
    "x-request-id",
    "openai-processing-ms",
    "openai-version",
    "request-id",
    "x-codex-turn-state",
}


def bearer(request: Request) -> str | None:
    header = request.headers.get("authorization") or ""
    if not header.lower().startswith("bearer "):
        return None
    token = header[7:].strip()
    return token or None


def build_upstream_headers(request: Request, services: Services, request_id: str) -> dict[str, str]:
    """构造发往 Bridge 的请求头。"""

    headers: dict[str, str] = {}
    for name, value in request.headers.items():
        lower = name.lower()
        if lower in STRIPPED_REQUEST_HEADERS or lower in HOP_BY_HOP:
            continue
        # 逐跳头里 Connection 列出的字段也要剔除。
        connection_tokens = {
            token.strip().lower()
            for token in (request.headers.get("connection") or "").split(",")
            if token.strip()
        }
        if lower in connection_tokens:
            continue
        headers[name] = value

    headers["Authorization"] = f"Bearer {services.worker_key or ''}"
    headers["Content-Type"] = "application/json"
    headers["Accept"] = request.headers.get("accept", "application/json")
    # 要求 identity，避免解压后的 body 与旧 Content-Encoding 不匹配。
    headers["Accept-Encoding"] = "identity"
    # 网关自生成的追踪 ID 覆盖客户端伪造值。
    headers["X-Relay-Request-Id"] = request_id
    return headers


def build_downstream_headers(response: httpx.Response) -> dict[str, str]:
    allowed: dict[str, str] = {}
    for name, value in response.headers.items():
        if name.lower() in SAFE_RESPONSE_HEADERS:
            allowed[name] = value
    allowed.setdefault("Cache-Control", "no-store")
    return allowed


# --- 上游调用 -----------------------------------------------------------


def make_client(services: Services) -> httpx.AsyncClient:
    """专用连接池。模型长流不能阻塞心跳与监控。

    读超时按「上游无字节空闲超时」设置；整次请求的总时长上限由
    `_deadline_exceeded()` 与 `deadline_timeout()` 另外强制
    （见 01_DEVELOPMENT_SPEC.md 6.4：连接 5s / 空闲 300s / 总时长 1800s）。
    """

    config = services.config
    timeout = httpx.Timeout(
        connect=float(config.connect_timeout_seconds),
        read=float(config.stream_idle_timeout_seconds),
        write=float(config.stream_idle_timeout_seconds),
        pool=float(config.connect_timeout_seconds),
    )
    # 关闭自动重定向：未知 3xx 显式失败。
    return httpx.AsyncClient(
        timeout=timeout,
        follow_redirects=False,
        trust_env=False,
        limits=httpx.Limits(max_connections=16, max_keepalive_connections=8),
    )


def deadline_exceeded(services: Services, started_monotonic: float) -> bool:
    """是否已超过单次请求总时长上限。"""

    limit = float(services.config.request_deadline_seconds)
    if limit <= 0:
        return False
    return (time.monotonic() - started_monotonic) > limit


def remaining_deadline(services: Services, started_monotonic: float) -> float:
    """剩余可用秒数；已超出返回 0。"""

    limit = float(services.config.request_deadline_seconds)
    if limit <= 0:
        return float("inf")
    return max(0.0, limit - (time.monotonic() - started_monotonic))


async def deadline_chunks(upstream, services, started, key_id=None):
    from .key_wallet import availability, KeyStopped
    iterator = upstream.aiter_bytes().__aiter__()
    checked = 0.0
    while True:
        pending = None
        try:
            async with asyncio.timeout(remaining_deadline(services, started)):
                if key_id is None:
                    chunk = await anext(iterator)
                else:
                    pending = asyncio.create_task(anext(iterator))
                    while True:
                        if time.monotonic() - checked >= 0.5:
                            checked = time.monotonic()
                            code = availability(services.db, key_id)
                            if code:
                                raise KeyStopped(code)
                        done, _ = await asyncio.wait({pending}, timeout=0.5)
                        if done:
                            chunk = pending.result()
                            break
        except StopAsyncIteration:
            return
        finally:
            if pending is not None and not pending.done():
                pending.cancel()
                with anyio.move_on_after(2, shield=True):
                    await asyncio.gather(pending, return_exceptions=True)
        yield chunk


async def wallet_await(awaitable, services, key_id, started):
    """Stop requests waiting for their first headers as well as active streams."""
    from .key_wallet import availability
    pending = asyncio.ensure_future(awaitable)
    try:
        async with asyncio.timeout(remaining_deadline(services, started)):
            while True:
                code = availability(services.db, key_id)
                if code:
                    raise KeyStopped(code)
                done, _ = await asyncio.wait({pending}, timeout=0.5)
                if done:
                    return pending.result()
    finally:
        if not pending.done():
            pending.cancel()
            with anyio.move_on_after(2, shield=True):
                await asyncio.gather(pending, return_exceptions=True)


@asynccontextmanager
async def wallet_stream(client, services, key_id, started, *args, **kwargs):
    upstream_context = client.stream(*args, **kwargs)
    upstream = await wallet_await(upstream_context.__aenter__(), services, key_id, started)
    try:
        yield upstream
    finally:
        await close_upstream(client, upstream_context)


async def close_upstream(client, context=None):
    # Starlette disconnect cancellation uses an AnyIO cancel scope.
    with anyio.move_on_after(4, shield=True):
        try:
            if context is not None:
                await context.__aexit__(None, None, None)
        finally:
            await client.aclose()


def make_probe_client(services: Services) -> httpx.AsyncClient:
    config = services.config
    return httpx.AsyncClient(
        timeout=httpx.Timeout(
            connect=float(config.probe_timeout_seconds),
            read=float(config.probe_timeout_seconds),
            write=float(config.probe_timeout_seconds),
            pool=float(config.probe_timeout_seconds),
        ),
        follow_redirects=False,
        trust_env=False,
        limits=httpx.Limits(max_connections=4, max_keepalive_connections=2),
    )


async def probe_worker(services: Services) -> dict[str, Any]:
    """经隧道读取 Bridge 的脱敏 Health，用于隧道状态分项。"""

    worker_id = services.config.worker_id
    headers = {"Authorization": f"Bearer {services.worker_key or ''}", "Accept": "application/json"}
    try:
        async with asyncio.timeout(services.config.probe_timeout_seconds):
            async with make_probe_client(services) as client:
                async with client.stream("GET", f"{services.config.worker_base_url}/internal/v1/health", headers=headers) as response:
                    raw = bytearray()
                    async for chunk in response.aiter_bytes():
                        if len(raw) + len(chunk) > 65536:
                            raise ValueError("health_too_large")
                        raw.extend(chunk)
    except (httpx.HTTPError, TimeoutError, ValueError) as exc:
        worker_state.record_probe(services.db, worker_id, ok=False)
        return {"ok": False, "reason": type(exc).__name__}

    if response.status_code != 200:
        worker_state.record_probe(services.db, worker_id, ok=False)
        return {"ok": False, "reason": f"http_{response.status_code}"}

    try:
        from .models import Health

        payload = contracts.load_json(raw)
        contracts.validate("health", payload)
        health = Health.model_validate(payload)
    except Exception:
        worker_state.record_probe(services.db, worker_id, ok=False)
        return {"ok": False, "reason": "invalid_health_payload"}

    if health.worker_id != worker_id:
        worker_state.record_probe(services.db, worker_id, ok=False)
        return {"ok": False, "reason": "worker_id_mismatch"}

    worker_state.record_probe(services.db, worker_id, ok=True, instance_id=health.instance_id)
    return {"ok": True, "instance_id": health.instance_id}


async def maybe_probe(services: Services) -> None:
    """限流探测：读路径触发，避免长流阻塞心跳。"""

    if not worker_state.should_probe(
        services.db, services.config.worker_id, services.config.probe_interval_seconds
    ):
        return
    try:
        await probe_worker(services)
    except Exception:
        # 探测失败不能影响用户请求。
        pass


# --- 计量落库 -----------------------------------------------------------


def persist_result(
    services: Services,
    *,
    request_id: str,
    attempt_id: str,
    client_key_id: str,
    requested_model: str | None,
    stream: bool,
    http_status: int | None,
    response_status: str,
    error_code: str | None,
    error_class: str | None,
    started_at: str,
    started_monotonic: float,
    first_byte_monotonic: float | None,
    meter: stream_meter.StreamMeterResult | None,
    usage_source: str | None,
    terminal_state: str | None,
) -> None:
    """把一次请求的结果与会话计量写入数据库。

    计量写入失败必须抛出 DatabaseError，由调用方转成 503。
    """

    ended = time.monotonic()
    duration_ms = int((ended - started_monotonic) * 1000)
    first_byte_ms = (
        int((first_byte_monotonic - started_monotonic) * 1000)
        if first_byte_monotonic is not None
        else None
    )

    usage = meter.usage if meter is not None else None
    usage_status = usage.status if usage is not None else "missing"

    # 估价：使用本次请求的 response model 与 usage。
    model_for_price = None
    if meter is not None:
        model_for_price = meter.response_model or requested_model
    else:
        model_for_price = requested_model

    estimate = pricing_mod.EstimateResult("unavailable", None, None, None, "no_usage")
    if usage is not None:
        estimate = services.price_book.estimate(
            usage, model=model_for_price, at=started_at
        )

    row_usage = (
        usage.as_columns()
        if usage is not None
        else {
            "input_tokens": None,
            "cached_input_tokens": None,
            "cache_write_tokens": None,
            "output_tokens": None,
            "reasoning_tokens": None,
            "total_tokens": None,
            "usage_status": "missing",
        }
    )

    conflict = 1 if (meter is not None and meter.conflict) else 0
    if meter is not None and meter.conflict:
        row_usage["usage_status"] = "invalid"
        estimate = pricing_mod.EstimateResult("unavailable", None, None, None, "terminal_conflict")

    with services.db.write() as conn:
        # 幂等：同一 attempt 已入账就不再重复计数，避免重复终态事件双计。
        attempt_row = conn.execute(
            "SELECT usage_recorded FROM attempts WHERE attempt_id = ?", (attempt_id,)
        ).fetchone()
        already_recorded = bool(attempt_row and attempt_row["usage_recorded"])

        if not already_recorded:
            conn.execute(
                "UPDATE requests SET ended_at = ?, duration_ms = ?, first_byte_ms = ?, http_status = ?, "
                "response_status = ?, error_code = ?, error_class = ?, response_model = COALESCE(?, response_model), "
                "upstream_response_id = COALESCE(?, upstream_response_id), "
                "input_tokens = ?, cached_input_tokens = ?, cache_write_tokens = ?, output_tokens = ?, "
                "reasoning_tokens = ?, total_tokens = ?, usage_status = ?, usage_source = ?, "
                "price_version = ?, estimated_cost = ?, estimate_status = ?, currency = ?, usage_conflict = ? "
                "WHERE request_id = ?",
                (
                    utc_now(),
                    duration_ms,
                    first_byte_ms,
                    http_status,
                    response_status,
                    error_code,
                    error_class,
                    meter.response_model if meter else None,
                    meter.response_id if meter else None,
                    row_usage["input_tokens"],
                    row_usage["cached_input_tokens"],
                    row_usage["cache_write_tokens"],
                    row_usage["output_tokens"],
                    row_usage["reasoning_tokens"],
                    row_usage["total_tokens"],
                    row_usage["usage_status"],
                    usage_source,
                    estimate.price_version,
                    estimate.display_cost,
                    estimate.status,
                    estimate.currency,
                    conflict,
                    request_id,
                ),
            )
            conn.execute(
                "UPDATE attempts SET ended_at = ?, upstream_response_id = ?, terminal_state = ?, "
                "terminal_seen = ?, usage_recorded = 1, http_status = ?, bytes_relayed = ? "
                "WHERE attempt_id = ?",
                (
                    utc_now(),
                    meter.response_id if meter else None,
                    terminal_state,
                    1 if (meter and meter.terminal_seen) else 0,
                    http_status,
                    meter.bytes_seen if meter else 0,
                    attempt_id,
                ),
            )
            _update_rollup(conn, request_id)
            if http_status == 200 or (usage is not None and usage.status == "reported"):
                from .key_wallet import settle
                settle(conn, client_key_id, request_id, estimate, usage)

    if estimate.reasoning in ("no_matching_price_rule",):
        audit(
            services.db,
            "pricing",
            "no_rule",
            detail={"model": (model_for_price or "")[:96]},
        )


def _update_rollup(conn: Any, request_id: str) -> None:
    """按日汇总（估价用 Decimal 累加，不逐条舍入）。"""

    row = conn.execute(
        "SELECT DATE(started_at) AS day, client_key_id, COALESCE(response_model, requested_model, '') AS model, "
        "input_tokens, cached_input_tokens, output_tokens, reasoning_tokens, total_tokens, "
        "usage_status, response_status, estimated_cost, currency "
        "FROM requests WHERE request_id = ?",
        (request_id,),
    ).fetchone()
    if row is None:
        return
    day = row["day"]
    succeeded = 1 if row["response_status"] == "completed" else 0
    failed = 1 if row["response_status"] in ("failed", "incomplete", "interrupted") else 0
    unknown = 1 if row["usage_status"] in ("missing", "invalid", "partial") else 0
    cost = row["estimated_cost"] or "0"
    currency = row["currency"] or "USD"

    # 用 Decimal 读回旧值 → 相加 → 写回字符串，避免浮点误差。
    from decimal import Decimal, InvalidOperation

    existing = conn.execute(
        "SELECT estimated_cost FROM daily_rollups WHERE day = ? AND client_key_id = ? AND model = ?",
        (day, row["client_key_id"], row["model"]),
    ).fetchone()
    try:
        previous = Decimal(existing["estimated_cost"]) if existing else Decimal(0)
        current = Decimal(cost)
    except (InvalidOperation, TypeError):
        previous, current = Decimal(0), Decimal(0)
    new_cost = str(previous + current)

    conn.execute(
        "INSERT INTO daily_rollups (day, client_key_id, model, request_count, success_count, failed_count, "
        "unknown_usage_count, input_tokens, cached_input_tokens, output_tokens, reasoning_tokens, total_tokens, "
        "estimated_cost, currency, updated_at) "
        "VALUES (?,?,?,1,?,?,?,?,?,?,?,?,?,?,?) "
        "ON CONFLICT(day, client_key_id, model) DO UPDATE SET "
        "request_count = request_count + 1, "
        "success_count = success_count + excluded.success_count, "
        "failed_count = failed_count + excluded.failed_count, "
        "unknown_usage_count = unknown_usage_count + excluded.unknown_usage_count, "
        "input_tokens = input_tokens + excluded.input_tokens, "
        "cached_input_tokens = cached_input_tokens + excluded.cached_input_tokens, "
        "output_tokens = output_tokens + excluded.output_tokens, "
        "reasoning_tokens = reasoning_tokens + excluded.reasoning_tokens, "
        "total_tokens = total_tokens + excluded.total_tokens, "
        "estimated_cost = ?, updated_at = ?",
        (
            day,
            row["client_key_id"],
            row["model"],
            succeeded,
            failed,
            unknown,
            row["input_tokens"] or 0,
            row["cached_input_tokens"] or 0,
            row["output_tokens"] or 0,
            row["reasoning_tokens"] or 0,
            row["total_tokens"] or 0,
            cost,
            currency,
            utc_now(),
            new_cost,
            utc_now(),
        ),
    )


# --- 请求处理 -----------------------------------------------------------


async def read_bounded_body(request: Request, limit: int) -> bytes:
    """读取有界请求体。压缩输入直接拒绝（避免解压炸弹）。"""

    encoding = (request.headers.get("content-encoding") or "identity").strip().lower()
    if encoding not in ("", "identity"):
        raise err.unsupported_media_type()

    content_length = request.headers.get("content-length")
    if content_length is not None:
        try:
            length = int(content_length)
            if length < 0:
                raise ValueError("negative length")
            if length > limit:
                raise err.payload_too_large(limit)
        except ValueError as exc:
            raise err.invalid_request("Invalid Content-Length header.") from exc

    body = bytearray()
    async for chunk in request.stream():
        if len(body) + len(chunk) > limit:
            raise err.payload_too_large(limit)
        body.extend(chunk)
    return bytes(body)


async def handle_models(request: Request, services: Services, client_key: Any) -> Response:
    """GET /v1/models：经隧道取回本机允许列表。"""

    config = services.config
    await maybe_probe(services)
    snapshot = worker_state.evaluate(services.db, config.worker_id, config)
    trace = context(request)
    record_worker_facts(trace, snapshot, services, client_key)
    servable, reason = worker_state.is_servable(snapshot)
    if not servable:
        raise _error_for_reason(reason)

    headers = {
        "Authorization": f"Bearer {services.worker_key or ''}",
        "Accept": "application/json",
        "Accept-Encoding": "identity",
        "X-Relay-Request-Id": context(request).get("request_id", str(uuid.uuid4())),
    }
    try:
        # Health has a strict 2s deadline. Catalog may reach the provider and
        # must not inherit that deadline or invalidate a healthy tunnel.
        async with asyncio.timeout(15), make_client(services) as client:
            async with client.stream("GET", f"{config.worker_base_url}/v1/models", headers=headers) as response:
                raw = bytearray()
                async for chunk in response.aiter_bytes():
                    if len(raw) + len(chunk) > 1024 * 1024:
                        raise httpx.ProtocolError("catalog_too_large")
                    raw.extend(chunk)
    except (httpx.HTTPError, TimeoutError) as exc:
        trace["outcome"] = error_kind(exc)
        raise err.worker_unavailable() from exc

    if response.status_code != 200:
        raise _map_upstream_error(response.status_code)

    try:
        payload = contracts.load_json(raw)
    except ValueError as exc:
        raise err.upstream_protocol_error() from exc

    # 只返回允许列表内的模型；不夹带账号或内部字段。
    data = payload.get("data") if isinstance(payload, dict) else None
    if not isinstance(data, list):
        raise err.upstream_protocol_error()

    allowlist = set(config.models_allowlist)
    filtered = []
    for item in data:
        if not isinstance(item, dict):
            continue
        model_id = item.get("id")
        if not isinstance(model_id, str):
            continue
        if model_id not in allowlist:
            continue
        filtered.append({"id": model_id, "object": "model", "owned_by": "codex-relay"})

    return JSONResponse(content={"object": "list", "data": filtered})


def record_worker_facts(trace, snapshot, services, client_key):
    trace.update(worker_state=snapshot.state, heartbeat_fresh=snapshot.heartbeat_fresh,
        probe_fresh=snapshot.probe_fresh, instance_consistent=snapshot.instance_consistent,
        adapter_ready=snapshot.adapter_ready, auth_state=snapshot.auth_state,
        bridge_ready=snapshot.bridge_ready, heartbeat_age_seconds=snapshot.heartbeat_age_seconds,
        global_limit=services.gate.global_limit, client_limit=services.gate.per_client_limit,
        global_active=services.gate.global_active, client_active=services.gate.active_for(client_key.key_id))


async def _fail_probe(services: Services) -> None:
    worker_state.record_probe(services.db, services.config.worker_id, ok=False)


def _error_for_reason(reason: str | None) -> err.RelayError:
    if reason == "adapter_unavailable":
        return err.adapter_unavailable()
    if reason == "upstream_reauth_required":
        return err.upstream_reauth_required()
    if reason == "worker_state_unknown":
        return err.worker_unavailable()
    return err.worker_unavailable()


def _map_upstream_error(status_code: int, retry_after: str | None = None, bridge_code: str | None = None) -> err.RelayError:
    """把 Bridge / 适配器的状态映射为对外语义（含脱敏类别）。"""

    if status_code == 402 and bridge_code == "upstream_quota_exhausted":
        return err.RelayError(402, "upstream_quota_exhausted", "Upstream subscription quota is unavailable or exhausted.")
    if status_code == 503 and bridge_code in {"upstream_connect_failed", "upstream_unavailable", "upstream_overloaded"}:
        return err.RelayError(503, bridge_code,
            "Windows relay could not connect to upstream." if bridge_code == "upstream_connect_failed"
            else "Upstream is temporarily unavailable.",
            retry_after=int(retry_after) if retry_after and retry_after.isdigit() and len(retry_after) <= 128 else None)
    if status_code == 429:
        return err.RelayError(
            429,
            "upstream_rate_limited",
            "Upstream subscription rate limit reached.",
            error_type="rate_limit_error",
            retry_after=int(retry_after) if retry_after and retry_after.isdigit() and len(retry_after) <= 128 else 5,
        )
    if status_code in (401, 403):
        return err.upstream_reauth_required()
    if status_code == 404:
        return err.upstream_protocol_error()
    if status_code == 503:
        return err.adapter_unavailable()
    if status_code == 504:
        return err.upstream_timeout()
    if 300 <= status_code < 400:
        # 不跟随重定向，未知 3xx 显式失败。
        return err.upstream_protocol_error()
    if status_code >= 500:
        return err.upstream_protocol_error()
    return err.RelayError(
        status_code if 400 <= status_code < 500 else 502,
        "upstream_request_rejected",
        "Upstream rejected the request.",
        error_type="invalid_request_error",
    )


async def handle_responses(
    request: Request, services: Services, client_key: Any
) -> Response:
    """POST /v1/responses。流式与非流式共用一条出站路径。"""

    config = services.config
    body = await read_bounded_body(request, config.max_body_bytes)

    try:
        parsed = contracts.load_json(body)
    except (ValueError, UnicodeError, RecursionError) as exc:
        raise err.invalid_request("Request body is not valid JSON.") from exc
    if not isinstance(parsed, dict):
        raise err.invalid_request("Request body must be a JSON object.")

    requested_model = parsed.get("model")
    if not isinstance(requested_model, str) or not requested_model:
        raise err.invalid_request("Request body must include a model string.")

    if requested_model not in set(config.models_allowlist):
        raise err.model_not_allowed(requested_model)

    if "stream" in parsed and not isinstance(parsed["stream"], bool):
        raise err.invalid_request("stream must be a boolean.")
    stream = bool(parsed.get("stream"))

    await maybe_probe(services)
    snapshot = worker_state.evaluate(services.db, config.worker_id, config)
    servable, reason = worker_state.is_servable(snapshot)
    trace = context(request)
    record_worker_facts(trace, snapshot, services, client_key)
    # The snapshot is also recorded for requests rejected before admission.
    if "session_fp" not in trace and isinstance(parsed.get("prompt_cache_key"), str) and len(parsed["prompt_cache_key"]) <= 8192:
        trace["session_fp"] = services.pepper.digest("diagnostic-session_fp\0"+parsed["prompt_cache_key"])[:32]
    if not servable:
        raise _error_for_reason(reason)

    # --- 并发闸门 ---------------------------------------------------
    from .key_wallet import require_available
    require_available(services.db, client_key.key_id)
    gate_reason = services.gate.try_acquire(client_key.key_id)  # type: ignore[union-attr]
    if gate_reason is not None:
        raise err.gateway_concurrency_limit()

    request_id = trace.setdefault("request_id", str(uuid.uuid4()))
    trace.update(global_active=services.gate.global_active, client_active=services.gate.active_for(client_key.key_id))
    attempt_id = str(uuid.uuid4())
    started_at = utc_now()
    started_monotonic = time.monotonic()

    # --- 落库请求骨架；失败则拒绝服务，避免“无计量保证”----------------
    try:
        _create_request_rows(
            services,
            request_id=request_id,
            attempt_id=attempt_id,
            client_key_id=client_key.key_id,
            client_alias=client_key.alias,
            requested_model=requested_model,
            stream=stream,
            started_at=started_at,
        )
    except Exception as exc:
        services.gate.release(client_key.key_id)  # type: ignore[union-attr]
        raise err.metering_unavailable() from exc

    headers = build_upstream_headers(request, services, request_id)
    url = f"{config.worker_base_url}/v1/responses"

    if not stream:
        return await _handle_nonstream(
            services,
            url=url,
            headers=headers,
            body=body,
            request_id=request_id,
            attempt_id=attempt_id,
            client_key=client_key,
            requested_model=requested_model,
            started_at=started_at,
            started_monotonic=started_monotonic,
            trace=trace,
        )

    return await _handle_stream(
        services,
        url=url,
        headers=headers,
        body=body,
        request_id=request_id,
        attempt_id=attempt_id,
        client_key=client_key,
        requested_model=requested_model,
        started_at=started_at,
        started_monotonic=started_monotonic,
        trace=trace,
    )


def _create_request_rows(
    services: Services,
    *,
    request_id: str,
    attempt_id: str,
    client_key_id: str,
    client_alias: str,
    requested_model: str,
    stream: bool,
    started_at: str,
) -> None:
    with services.db.write() as conn:
        conn.execute(
            "INSERT INTO requests (request_id, worker_id, client_key_id, client_alias, requested_model, "
            "stream, started_at, usage_status, estimate_status, created_at) "
            "VALUES (?,?,?,?,?,?,?, 'missing', 'unavailable', ?)",
            (
                request_id,
                services.config.worker_id,
                client_key_id,
                client_alias,
                requested_model,
                1 if stream else 0,
                started_at,
                started_at,
            ),
        )
        conn.execute(
            "INSERT INTO attempts (attempt_id, request_id, attempt_no, started_at) VALUES (?,?,1,?)",
            (attempt_id, request_id, started_at),
        )


async def _handle_nonstream(
    services: Services,
    *,
    url: str,
    headers: dict[str, str],
    body: bytes,
    request_id: str,
    attempt_id: str,
    client_key: Any,
    requested_model: str,
    started_at: str,
    started_monotonic: float,
    trace: dict | None = None,
) -> Response:
    config = services.config
    trace = trace if trace is not None else {}
    # 并发槽必须在所有退出路径上恰好释放一次。
    try:
        first_byte: float | None = None
        if deadline_exceeded(services, started_monotonic):
            _finalize_error(services, request_id, attempt_id, client_key, "upstream_timeout",
                            "request_deadline_exceeded", started_at, started_monotonic, None)
            raise err.upstream_timeout()

        # 总时长上限：把本次出站调用限制在剩余额度内，避免无限等待。
        budget = remaining_deadline(services, started_monotonic)
        call_timeout = httpx.Timeout(
            connect=float(config.connect_timeout_seconds),
            read=min(float(config.stream_idle_timeout_seconds), budget),
            write=min(float(config.stream_idle_timeout_seconds), budget),
            pool=float(config.connect_timeout_seconds),
        )
        try:
            async with asyncio.timeout(budget), make_client(services) as client:
                async with wallet_stream(
                    client, services, client_key.key_id, started_monotonic,
                    "POST", url, headers=headers, content=body, timeout=call_timeout
                ) as upstream:
                    services.connection_log.emit("worker_opened", request_id=request_id, http_status=upstream.status_code,
                        headers_ms=(time.monotonic()-started_monotonic)*1000)
                    chunks: list[bytes] = []
                    size = 0
                    async for chunk in deadline_chunks(upstream, services, started_monotonic, client_key.key_id):
                        if first_byte is None:
                            first_byte = time.monotonic()
                        size += len(chunk)
                        if size > config.max_body_bytes:
                            raise httpx.ProtocolError("response_too_large")
                        chunks.append(chunk)
                    raw = b"".join(chunks)
                    status = upstream.status_code
                    downstream_headers = build_downstream_headers(upstream)
        except KeyStopped as exc:
            trace.update(error_code=exc.code, outcome="key_disabled" if exc.code == "invalid_api_key" else "key_balance_exhausted")
            status = 401 if exc.code == "invalid_api_key" else 503 if exc.code == "balance_metering_unavailable" else 402
            _finalize_error(services, request_id, attempt_id, client_key, exc.code,
                            "key_stopped", started_at, started_monotonic, status)
            raise err.RelayError(status, exc.code, "API key is no longer available.") from exc
        except httpx.ConnectError as exc:
            trace["outcome"] = error_kind(exc)
            _finalize_error(services, request_id, attempt_id, client_key, "worker_unavailable",
                            "connect_error", started_at, started_monotonic, 503)
            raise err.worker_unavailable() from exc
        except (httpx.TimeoutException, TimeoutError) as exc:
            trace["outcome"] = error_kind(exc)
            _finalize_error(services, request_id, attempt_id, client_key, "upstream_timeout", "timeout",
                            started_at, started_monotonic, 504)
            raise err.upstream_timeout() from exc
        except httpx.HTTPError as exc:
            trace["outcome"] = error_kind(exc)
            _finalize_error(services, request_id, attempt_id, client_key, "upstream_protocol_error",
                            type(exc).__name__, started_at, started_monotonic, 502)
            raise err.upstream_protocol_error() from exc

        if status != 200:
            relay_error = _map_upstream_error(status, upstream.headers.get("retry-after"), upstream.headers.get("x-relay-error-code"))
            _finalize_error(
                services, request_id, attempt_id, client_key, relay_error.code, "upstream_status",
                started_at, started_monotonic, status,
            )
            raise relay_error

        # 解析非流式 JSON 的 usage。
        meter = stream_meter.StreamMeterResult()
        response_status = "completed"
        try:
            payload = json.loads(raw)
            if isinstance(payload, dict):
                meter.response_id = payload.get("id") if isinstance(payload.get("id"), str) else None
                meter.response_model = (
                    payload.get("model") if isinstance(payload.get("model"), str) else None
                )
                status_value = payload.get("status")
                if isinstance(status_value, str):
                    response_status = status_value
                usage_payload = payload.get("usage")
                if isinstance(usage_payload, dict):
                    from .models import extract_usage

                    meter.usage = extract_usage(usage_payload)
                    meter.usage_source = "response_body"
                    meter.terminal_seen = True
                else:
                    from .models import NormalizedUsage

                    meter.usage = NormalizedUsage(status="missing", problem="response_without_usage")
        except json.JSONDecodeError:
            # 上游返回了非 JSON 的 200：协议异常，不猜测内容。
            _finalize_error(services, request_id, attempt_id, client_key, "upstream_protocol_error",
                            "invalid_json", started_at, started_monotonic, 200)
            raise err.upstream_protocol_error()

        try:
            persist_result(
                services,
                request_id=request_id,
                attempt_id=attempt_id,
                client_key_id=client_key.key_id,
                requested_model=requested_model,
                stream=False,
                http_status=200,
                response_status=response_status,
                error_code=None,
                error_class=None,
                started_at=started_at,
                started_monotonic=started_monotonic,
                first_byte_monotonic=first_byte,
                meter=meter,
                usage_source=meter.usage_source,
                terminal_state=response_status,
            )
        except Exception as exc:
            raise err.metering_unavailable() from exc

        return Response(content=raw, status_code=200, headers=downstream_headers, media_type=None)
    except asyncio.CancelledError:
        _finalize_error(services, request_id, attempt_id, client_key, "client_disconnected",
                        "cancelled", started_at, started_monotonic, None)
        raise
    finally:
        # A 200 whose body was interrupted may still have incurred provider usage.
        # Freeze finite wallets for review rather than silently making it free.
        try:
            if "upstream" in locals() and upstream.status_code == 200:
                from .key_wallet import settle
                with services.db.write() as conn:
                    settle(conn, client_key.key_id, request_id,
                           pricing_mod.EstimateResult("unavailable", None, None, None, "no_usage"), None)
        finally:
            services.gate.release(client_key.key_id)  # type: ignore[union-attr]


async def _handle_stream(
    services: Services,
    *,
    url: str,
    headers: dict[str, str],
    body: bytes,
    request_id: str,
    attempt_id: str,
    client_key: Any,
    requested_model: str,
    started_at: str,
    started_monotonic: float,
    trace: dict | None = None,
) -> Response:
    """流式转发。上游上下文必须保持到下游流结束。"""

    client = make_client(services)
    trace = trace if trace is not None else {}
    try:
        upstream_ctx = client.stream("POST", url, headers=headers, content=body)
        async with asyncio.timeout(remaining_deadline(services, started_monotonic)):
            upstream = await wallet_await(upstream_ctx.__aenter__(), services, client_key.key_id, started_monotonic)
        services.connection_log.emit("worker_opened", request_id=request_id, http_status=upstream.status_code,
            headers_ms=(time.monotonic()-started_monotonic)*1000)
    except KeyStopped as exc:
        await close_upstream(client)
        services.gate.release(client_key.key_id)
        status = 401 if exc.code == "invalid_api_key" else 503 if exc.code == "balance_metering_unavailable" else 402
        trace.update(error_code=exc.code, outcome="key_disabled" if exc.code == "invalid_api_key" else "key_balance_exhausted")
        _finalize_error(services, request_id, attempt_id, client_key, exc.code, "key_stopped",
                        started_at, started_monotonic, status)
        raise err.RelayError(status, exc.code, "API key is no longer available.") from exc
    except httpx.ConnectError as exc:
        trace["outcome"] = error_kind(exc)
        await close_upstream(client)
        services.gate.release(client_key.key_id)  # type: ignore[union-attr]
        _finalize_error(services, request_id, attempt_id, client_key, "worker_unavailable", "connect_error",
                        started_at, started_monotonic, 503)
        raise err.worker_unavailable() from exc
    except (httpx.TimeoutException, TimeoutError) as exc:
        trace["outcome"] = error_kind(exc)
        await close_upstream(client)
        services.gate.release(client_key.key_id)  # type: ignore[union-attr]
        _finalize_error(services, request_id, attempt_id, client_key, "upstream_timeout", "timeout",
                        started_at, started_monotonic, 504)
        raise err.upstream_timeout() from exc
    except httpx.HTTPError as exc:
        trace["outcome"] = error_kind(exc)
        await close_upstream(client)
        services.gate.release(client_key.key_id)  # type: ignore[union-attr]
        _finalize_error(services, request_id, attempt_id, client_key, "upstream_protocol_error", type(exc).__name__,
                        started_at, started_monotonic, 502)
        raise err.upstream_protocol_error() from exc
    except asyncio.CancelledError:
        try:
            await close_upstream(client)
        finally:
            services.gate.release(client_key.key_id)
            _finalize_error(services, request_id, attempt_id, client_key, "client_disconnected",
                            "cancelled", started_at, started_monotonic, None)
        raise

    if upstream.status_code != 200:
        await close_upstream(client, upstream_ctx)
        services.gate.release(client_key.key_id)  # type: ignore[union-attr]
        relay_error = _map_upstream_error(upstream.status_code, upstream.headers.get("retry-after"), upstream.headers.get("x-relay-error-code"))
        _finalize_error(services, request_id, attempt_id, client_key, relay_error.code, "upstream_status",
                        started_at, started_monotonic, upstream.status_code)
        raise relay_error

    downstream_headers = build_downstream_headers(upstream)
    meter = stream_meter.SSEMeter()
    state: dict[str, Any] = {
        "first_byte": None,
        "interrupted": False,
        "closed": False,
        "deadline_exceeded": False,
        "outcome": None, "last_byte": None, "bytes": 0, "chunks": 0,
        "started": False,
    }

    async def relay() -> AsyncIterator[bytes]:
        state["started"] = True
        try:
            async for chunk in deadline_chunks(upstream, services, started_monotonic, client_key.key_id):
                if state["first_byte"] is None:
                    state["first_byte"] = time.monotonic()
                state["last_byte"] = time.monotonic()
                state["bytes"] += len(chunk)
                state["chunks"] += 1
                # 先喂旁路解析器，再原样透传；解析绝不篡改字节。
                meter.feed(chunk)
                yield chunk
                # 总时长上限：已发出 200 头时只能关闭连接，不能改状态码。
                if deadline_exceeded(services, started_monotonic):
                    state["interrupted"] = True
                    state["deadline_exceeded"] = True
                    return
        except KeyStopped as exc:
            state["interrupted"] = True
            state["outcome"] = "key_disabled" if exc.code == "invalid_api_key" else "key_balance_exhausted"
            state["key_error"] = exc.code
            trace["error_code"] = exc.code
            raise
        except TimeoutError:
            state["interrupted"] = True
            state["deadline_exceeded"] = True
            raise
        except (httpx.HTTPError, GeneratorExit, asyncio.CancelledError, ValueError) as exc:
            state["interrupted"] = True
            state["outcome"] = error_kind(exc)
            raise
        finally:
            await finish_stream()

    async def finish_stream():
        if state["closed"]:
            return
        state["closed"] = True
        result = meter.finish()
        outcome = "request_deadline_exceeded" if state["deadline_exceeded"] else state["outcome"] if state["interrupted"] else (
            "response_complete" if result.terminal_state == "completed" else "response_failed" if result.terminal_state == "failed" else "response_incomplete" if result.terminal_seen else "no_terminal")
        if not state["started"]: outcome = "stream_not_started"
        trace.update(outcome=outcome, terminal_seen=result.terminal_seen)
        services.connection_log.emit("stream_finished", request_id=request_id, outcome=outcome,
            client_fp=trace.get("client_fp"), error_code=state.get("key_error"),
            terminal_seen=result.terminal_seen, terminal_conflict=result.conflict,
            bytes_received=state["bytes"], chunks_received=state["chunks"],
            first_byte_ms=(state["first_byte"]-started_monotonic)*1000 if state["first_byte"] else None,
            last_byte_ms=(state["last_byte"]-started_monotonic)*1000 if state["last_byte"] else None,
            duration_ms=(time.monotonic()-started_monotonic)*1000)
        try:
            await close_upstream(client, upstream_ctx)
        except Exception:
            pass

        if state["interrupted"]:
            # 已发送 200 头就不能改状态；关闭连接并记录 interrupted。
            terminal = None
            response_status = "interrupted"
            if state.get("key_error"):
                error_code = state["key_error"]
                error_class = "key_stopped"
            elif state.get("deadline_exceeded"):
                error_code = "request_deadline_exceeded"
                error_class = "deadline_exceeded"
            else:
                error_code = "upstream_stream_interrupted"
                error_class = "interrupted"
        else:
            terminal = result.terminal_state
            response_status = terminal or ("interrupted" if not result.terminal_seen else "unknown")
            error_code = result.upstream_error_code
            error_class = None if result.terminal_seen else "stream_ended_without_terminal"

        try:
            persist_result(
                services,
                request_id=request_id,
                attempt_id=attempt_id,
                client_key_id=client_key.key_id,
                requested_model=requested_model,
                stream=True,
                http_status=200,
                response_status=response_status,
                error_code=error_code,
                error_class=error_class,
                started_at=started_at,
                started_monotonic=started_monotonic,
                first_byte_monotonic=state["first_byte"],
                meter=result,
                usage_source=result.usage_source,
                terminal_state=terminal,
            )
        except Exception:
            # 流已开始，不能改成 503；记录计量异常以便审计。
            audit(
                services.db,
                "metering",
                "post_stream_write_failed",
                detail={"request_id": request_id},
            )
        finally:
            services.gate.release(client_key.key_id)  # type: ignore[union-attr]
    return StreamingResponse(relay(), status_code=200, headers=downstream_headers, media_type=None, background=BackgroundTask(finish_stream))


def _finalize_error(
    services: Services,
    request_id: str,
    attempt_id: str,
    client_key: Any,
    error_code: str,
    error_class: str,
    started_at: str,
    started_monotonic: float,
    http_status: int | None,
) -> None:
    """记录“尚未发送响应头就失败”的请求。"""

    try:
        persist_result(
            services,
            request_id=request_id,
            attempt_id=attempt_id,
            client_key_id=client_key.key_id,
            requested_model=None,
            stream=False,
            http_status=http_status,
            response_status="failed",
            error_code=error_code,
            error_class=error_class,
            started_at=started_at,
            started_monotonic=started_monotonic,
            first_byte_monotonic=None,
            meter=None,
            usage_source=None,
            terminal_state=None,
        )
    except Exception:
        audit(
            services.db,
            "metering",
            "finalize_failed",
            detail={"request_id": request_id, "error_code": error_code},
        )
