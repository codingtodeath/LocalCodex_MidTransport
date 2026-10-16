import asyncio
import hmac
import json
import re
import uuid
from contextlib import asynccontextmanager

import httpx
import anyio
from fastapi import FastAPI, Request
from starlette.responses import JSONResponse, Response, StreamingResponse

from .config import read_key
from .contracts import validate
from .proxy_adapter import ProxyAdapter
from .sse import SSEFramer, safe_errors
from .state import utcnow
from .routing import routing_headers

ROUTES = {b"/v1/models": "GET", b"/v1/responses": "POST", b"/internal/v1/health": "GET"}


def error(status, code, retry=None):
    return JSONResponse({"error": {"message": code.replace("_", " "),
                                  "type": "server_error", "code": code}}, status_code=status,
                        headers={"X-Relay-Error-Code": code, **({"Retry-After": str(retry)} if retry else {})})


class Boundary:
    """Validate raw path before any routing/normalization. No forwarded header trust."""
    def __init__(self, app, key):
        self.app, self.key = app, key.encode("ascii")

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http":
            if scope["type"] == "websocket":
                await send({"type": "websocket.close", "code": 1008})
                return
            return await self.app(scope, receive, send)
        path = scope.get("raw_path", scope["path"].encode())
        if path not in ROUTES or scope.get("query_string"):
            return await error(404, "not_found")(scope, receive, send)
        if scope["method"] != ROUTES[path]:
            return await error(405, "method_not_allowed")(scope, receive, send)
        headers = scope["headers"]
        auth = [v for k, v in headers if k.lower() == b"authorization"]
        if len(auth) != 1 or not hmac.compare_digest(auth[0], b"Bearer " + self.key):
            return await error(401, "invalid_api_key")(scope, receive, send)
        return await self.app(scope, receive, send)


async def bounded_read(response, limit):
    data = bytearray()
    async for chunk in response.aiter_bytes():
        data.extend(chunk)
        if len(data) > limit:
            raise ValueError("upstream_body_too_large")
    return bytes(data)


async def close_response(response):
    # Starlette disconnect cancellation must not interrupt connection cleanup.
    with anyio.move_on_after(4, shield=True):
        await response.aclose()


async def open_until_disconnect(adapter, request, body):
    """Cancellation of the gateway/Key also cancels a pending retry on Windows."""
    task = asyncio.create_task(adapter.open("POST", "/v1/responses", body, request_headers=request.headers))
    handed_off = False
    try:
        while True:
            done, _ = await asyncio.wait({task}, timeout=.2)
            if done:
                response = await task
                handed_off = True
                return response
            if await request.is_disconnected():
                raise asyncio.CancelledError()
    finally:
        if not task.done():
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
        if not handed_off and task.done() and not task.cancelled() and task.exception() is None:
            await close_response(task.result())


def create_app(config, state, adapter=None):
    if adapter is None and config.adapter.mode == "existing_codex_login":
        from .existing_adapter import ExistingAdapter
        adapter = ExistingAdapter(config, state)
    adapter = adapter or ProxyAdapter(config, state)

    @asynccontextmanager
    async def lifespan(app):
        state.bridge_ready = True
        try:
            yield
        finally:
            state.bridge_ready = False
            await adapter.close()

    app = FastAPI(lifespan=lifespan, docs_url=None, redoc_url=None, openapi_url=None)
    app.router.redirect_slashes = False
    app.add_middleware(Boundary, key=read_key(config.bridge.auth_secret_file, "wkr_"))
    from .runtime_diagnostics import BridgeObserver
    app.add_middleware(BridgeObserver, config=config, state=state)
    app.state.adapter = adapter

    @app.get("/internal/v1/health")
    async def health():
        try:
            async with asyncio.timeout(1):
                await adapter.probe()
        except TimeoutError:
            state.adapter_ready = False
        return validate("health", state.health())

    @app.get("/v1/models")
    async def models():
        upstream = None
        try:
            async with asyncio.timeout(config.bridge.connect_seconds + 2):
                upstream = await adapter.open("GET", "/v1/models")
                if upstream.status_code != 200:
                    return error(503, "adapter_unavailable")
                data = json.loads(await bounded_read(upstream, 1024 * 1024))
            # Rebuild catalog using a narrow list; no metadata or credential indexes.
            items = [{"id": x["id"], "object": "model", "created": 0, "owned_by": "private-relay"}
                     for x in data["data"] if isinstance(x, dict) and x.get("id") in config.models_allowlist]
            return {"object": "list", "data": items}
        except (TimeoutError, httpx.TimeoutException):
            return error(504, "upstream_timeout")
        except (httpx.HTTPError, ValueError, KeyError, TypeError):
            return error(503, "adapter_unavailable")
        finally:
            if upstream:
                await close_response(upstream)

    @app.post("/v1/responses")
    async def responses(request: Request):
        if request.headers.get("content-encoding", "identity").lower() != "identity":
            return error(415, "compressed_body_not_supported")
        if request.headers.get("content-type", "").split(";", 1)[0].strip().lower() != "application/json":
            return error(415, "json_required")
        lengths = request.headers.getlist("content-length")
        if lengths:
            try:
                if len(lengths) != 1 or int(lengths[0]) < 0:
                    return error(400, "invalid_content_length")
                if int(lengths[0]) > config.bridge.max_body_bytes:
                    return error(413, "request_too_large")
            except ValueError:
                return error(400, "invalid_content_length")
        if state.active_requests >= config.bridge.max_concurrency:
            return error(429, "gateway_concurrency_limit", retry=1)
        # All slots live in one event loop; check and increment contain no await.
        state.active_requests += 1
        upstream = None
        handed_off = False
        timeout = asyncio.timeout(config.bridge.total_seconds)
        try:
            await timeout.__aenter__()
            body = bytearray()
            async for chunk in request.stream():
                body.extend(chunk)
                if len(body) > config.bridge.max_body_bytes:
                    return error(413, "request_too_large")
            try:
                payload = json.loads(body, parse_constant=lambda _: (_ for _ in ()).throw(ValueError()))
            except (ValueError, UnicodeError, RecursionError):
                return error(400, "invalid_json")
            if not isinstance(payload, dict) or not isinstance(payload.get("model"), str) or not isinstance(payload.get("stream", False), bool):
                return error(400, "invalid_request")
            if payload["model"] not in config.models_allowlist:
                return error(403, "model_not_allowed")
            if any(isinstance(t, dict) and t.get("type") not in ("function", "custom") for t in payload.get("tools", []) or []):
                return error(403, "hosted_tools_not_supported")
            upstream = await open_until_disconnect(adapter, request, bytes(body))
            status = upstream.status_code
            if status in (401, 403):
                # Auth state stays independent of quota subprocess.
                state.auth_state = "reauth_required"
                # Server supplement §5.4 maps Bridge 401/403 to external 503 reauth.
                return error(401, "upstream_reauth_required")
            if status != 200:
                retry = upstream.headers.get("retry-after", "")
                from .safe_retry import retry_after
                import math
                wait = retry_after(retry)
                category = upstream.extensions.get("relay_error_category")
                if category == "quota_exhausted":
                    return error(402, "upstream_quota_exhausted")
                if status == 429:
                    return error(429, "upstream_rate_limited", retry=max(1, math.ceil(wait)) if wait is not None else 1)
                if status == 503:
                    return error(503, "upstream_overloaded" if category == "temporary_overload" else "upstream_unavailable",
                                 retry=max(1, math.ceil(wait)) if wait is not None else None)
                if status == 504:
                    return error(504, "upstream_timeout")
                if 300 <= status < 400:
                    return error(502, "upstream_protocol_error")
                return error(status if status in (400, 404, 422) else 502, "upstream_error")
            if upstream.headers.get("content-encoding", "identity").lower() != "identity":
                return error(502, "upstream_protocol_error")
            if not payload.get("stream", False):
                raw = await bounded_read(upstream, config.bridge.max_body_bytes)
                result = json.loads(raw)
                if not isinstance(result, dict):
                    return error(502, "upstream_protocol_error")
                if result.get("type") == "error":
                    return error(502, "upstream_error")
                if result.get("status") == "completed":
                    state.last_inference_success_at, state.auth_state = utcnow(), "ready"
                cleaned = safe_errors(result)
                return Response(raw if cleaned == result else json.dumps(cleaned, ensure_ascii=False).encode(),
                                media_type="application/json", headers=routing_headers(upstream.headers, response=True))
            if not upstream.headers.get("content-type", "").lower().startswith("text/event-stream"):
                return error(502, "upstream_protocol_error")
            # The iterator owns the upstream connection until disconnect, EOF or timeout.
            # Start a fresh timeout in the iterator using the remaining total budget.
            remaining = timeout.when() - asyncio.get_running_loop().time()
            await timeout.__aexit__(None, None, None)
            timeout = None
            handed_off = True

            async def stream():
                framer = SSEFramer()
                finished = False
                try:
                    async with asyncio.timeout(max(remaining, .001)):
                        async for chunk in upstream.aiter_raw():
                            for frame in framer.feed(chunk):
                                yield frame
                        # Preserve incomplete tail but do not manufacture a completed event.
                        if framer.buffer:
                            # Incomplete errors cannot safely be forwarded.
                            raise ValueError("truncated_sse_event")
                        finished = True
                except (httpx.HTTPError, TimeoutError, ValueError, UnicodeError) as exc:
                    from .runtime_diagnostics import diagnostics, exception_info
                    diagnostics(config,state).emit("bridge_stream_failed", request_id=request.headers.get("x-relay-request-id"), **exception_info(exc))
                    # Raise a constant error so ASGI closes a started stream as incomplete.
                    raise RuntimeError("upstream_stream_interrupted") from None
                finally:
                    try:
                        await close_response(upstream)
                    finally:
                        state.active_requests -= 1
                    if finished and framer.completed:
                        state.last_inference_success_at, state.auth_state = utcnow(), "ready"

            return StreamingResponse(stream(), media_type="text/event-stream",
                                     headers={"Cache-Control": "no-store", "X-Accel-Buffering": "no",
                                              **routing_headers(upstream.headers, response=True)})
        except (TimeoutError, httpx.TimeoutException):
            return error(504, "upstream_timeout")
        except httpx.ConnectError:
            if config.adapter.mode != "existing_codex_login":
                state.adapter_ready = False
            return error(503, "upstream_connect_failed")
        except (httpx.HTTPError, ValueError, TypeError, UnicodeError):
            return error(502, "upstream_protocol_error")
        except asyncio.CancelledError:
            if timeout and timeout.expired():
                # Manual timeout scope must translate cancellation before mapping 504.
                try:
                    await timeout.__aexit__(asyncio.CancelledError, asyncio.CancelledError(), None)
                except TimeoutError:
                    timeout = None
                    return error(504, "upstream_timeout")
            raise
        finally:
            if not handed_off:
                if timeout:
                    await timeout.__aexit__(None, None, None)
                if upstream:
                    try:
                        await close_response(upstream)
                    finally:
                        state.active_requests -= 1
                else:
                    state.active_requests -= 1
    return app
