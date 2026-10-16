"""FastAPI 应用装配。

路由角色隔离（见 01_DEVELOPMENT_SPEC.md 6.2、第 12 节）：
* `/v1/*`           只用 gw Key
* `/internal/v1/*`  只用 tel Key
* `/admin/api/*`    只用管理员会话

默认拒绝跨角色调用与未知路径。`/healthz` 只返回最小 liveness，
不含设备、版本、用量或密钥信息。
"""

from __future__ import annotations

import asyncio
import json
import time
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any, AsyncIterator

from fastapi import FastAPI, Request
from fastapi.responses import FileResponse, JSONResponse, Response
from starlette.exceptions import HTTPException as StarletteHTTPException

from . import api_proxy, auth as auth_mod, worker_state
from . import admin_api, telemetry_api
from .config import RelayConfig
from .db import Database, audit, cleanup
from .errors import RelayError
from .services import Services, build_services
from .connection_log import ConnectionObserver, context, CODES

WEB_DIST = Path(__file__).parent / "web" / "dist"

# 明确不存在的路径族：即使持有 wkr Key 也不可达（SEC04）。
BLOCKED_PREFIXES = (
    "/v0/",
    "/v8/",
    "/management",
    "/auth-files",
    "/app-server",
    "/debug",
    "/proxy",
    "/files",
)


def create_app(
    config: RelayConfig,
    *,
    services: Services | None = None,
    worker_key: str | None = None,
    create_missing_secrets: bool = False,
    rolling_activation_file: Path | None = None,
) -> FastAPI:
    if services is None:
        services = build_services(
            config,
            pepper_bytes=None,  # type: ignore[arg-type]
            session_secret=None,  # type: ignore[arg-type]
            worker_key=worker_key,
            create_missing_secrets=create_missing_secrets,
        )
    elif worker_key is not None:
        services.worker_key = worker_key

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        from .key_wallet import recover_pending
        recovered = recover_pending(services.db) if rolling_activation_file is None else 0
        # 启动时历史“在线”不能直接当作在线。
        if rolling_activation_file is None:
            worker_state.reset_after_restart(services.db)
        purge = auth_mod.purge_expired_sessions(services.db)
        removed = cleanup(services.db, config)
        audit(
            services.db,
            "service",
            "started",
            detail={"version": "1.0.0", "sessions_purged": purge, "cleanup": removed, "wallet_requests_recovered": recovered},
        )
        async def probe_loop():
            while True:
                if rolling_activation_file is not None and not rolling_activation_file.exists():
                    await asyncio.sleep(.1)
                    continue
                try:
                    await api_proxy.probe_worker(services)
                except Exception:
                    worker_state.record_probe(services.db, config.worker_id, ok=False)
                await asyncio.sleep(config.probe_interval_seconds)

        probe_task = asyncio.create_task(probe_loop(), name="bridge-health-probe")
        try:
            yield
        finally:
            probe_task.cancel()
            try:
                await probe_task
            except asyncio.CancelledError:
                pass
            audit(services.db, "service", "stopped")
            services.db.close()

    app = FastAPI(
        title="Codex Private Relay Gateway",
        version="1.0.0",
        docs_url=None,
        redoc_url=None,
        openapi_url=None,
        lifespan=lifespan,
    )
    app.state.services = services
    app.state.config = config
    app.state.db = services.db

    # --- 异常处理：统一兼容形状，绝不回显敏感内容 --------------------
    @app.exception_handler(RelayError)
    async def relay_error_handler(request: Request, exc: RelayError) -> JSONResponse:
        context(request)["error_code"] = exc.code
        headers = exc.response_headers()
        headers["X-Relay-Error-Code"] = exc.code if exc.code in CODES else "other"
        return JSONResponse(status_code=exc.status_code, content=exc.body(), headers=headers)

    @app.exception_handler(StarletteHTTPException)
    async def http_error_handler(request: Request, exc: StarletteHTTPException) -> JSONResponse:
        context(request)["error_code"] = "not_found" if exc.status_code == 404 else "request_failed"
        if exc.status_code == 404:
            return JSONResponse(
                status_code=404,
                content={
                    "error": {
                        "message": "Not found.",
                        "type": "invalid_request_error",
                        "code": "not_found",
                    }
                },
            )
        return JSONResponse(
            status_code=exc.status_code,
            content={
                "error": {
                    "message": "Request failed.",
                    "type": "invalid_request_error",
                    "code": "request_failed",
                }
            },
        )

    @app.exception_handler(Exception)
    async def unhandled_handler(request: Request, exc: Exception) -> JSONResponse:
        context(request)["error_code"] = "internal_error"
        # 不把异常原文返回给客户端；只记录类型。
        audit(services.db, "service", "unhandled_exception", detail={"type": type(exc).__name__})
        return JSONResponse(
            status_code=500,
            content={
                "error": {
                    "message": "Internal relay error.",
                    "type": "server_error",
                    "code": "internal_error",
                }
            },
        )

    # --- 安全响应头 + 阻断路径族 -------------------------------------
    @app.middleware("http")
    async def security_middleware(request: Request, call_next: Any) -> Response:
        path = request.url.path
        for prefix in BLOCKED_PREFIXES:
            if path.startswith(prefix):
                return JSONResponse(
                    status_code=404,
                    content={
                        "error": {
                            "message": "Not found.",
                            "type": "invalid_request_error",
                            "code": "not_found",
                        }
                    },
                )
        response: Response = await call_next(request)
        response.headers["X-Content-Type-Options"] = "nosniff"
        response.headers["Referrer-Policy"] = "no-referrer"
        if path.startswith(("/admin", "/v1", "/internal")):
            response.headers["Cache-Control"] = "no-store"
        return response

    app.include_router(telemetry_api.router)
    app.include_router(admin_api.router)
    _register_client_api(app, services)
    _register_health(app, services)
    _register_web(app)
    app.add_middleware(ConnectionObserver, services=services)
    return app


def _register_probe_hook(app: FastAPI, services: Services) -> None:
    """按 probe_interval 限流地经隧道探测 Bridge。

    放在后台任务里，**不能**阻塞请求路径或心跳：见 01_DEVELOPMENT_SPEC.md
    第 16 节“模型请求和控制/监控请求应使用独立连接池”。
    """

    @app.middleware("http")
    async def probe_middleware(request: Request, call_next: Any) -> Response:
        response: Response = await call_next(request)
        try:
            if worker_state.should_probe(
                services.db,
                services.config.worker_id,
                services.config.probe_interval_seconds,
            ):
                # 探测是只读操作，可以独立退避重试；失败不影响本次响应。
                asyncio.create_task(api_proxy.probe_worker(services))
        except Exception:
            pass
        return response


# --- 客户端模型接口 -----------------------------------------------------


def _register_client_api(app: FastAPI, services: Services) -> None:
    @app.get("/v1/models")
    async def models(request: Request) -> Response:
        client_key = _authenticate_client(request, services)
        return await api_proxy.handle_models(request, services, client_key)

    @app.post("/v1/responses")
    async def responses(request: Request) -> Response:
        client_key = _authenticate_client(request, services)
        return await api_proxy.handle_responses(request, services, client_key)

    @app.api_route("/v1/{rest:path}", methods=["GET", "POST", "PUT", "PATCH", "DELETE", "OPTIONS", "HEAD"])
    async def v1_unknown(request: Request, rest: str) -> Response:
        # 第一版不实现 Chat Completions / 文件 / 检索等路径。
        _authenticate_client(request, services)
        return JSONResponse(
            status_code=404,
            content={
                "error": {
                    "message": "This relay implements only /v1/models and /v1/responses.",
                    "type": "invalid_request_error",
                    "code": "not_found",
                }
            },
        )

    @app.api_route("/v1", methods=["GET", "POST"])
    async def v1_root(request: Request) -> Response:
        _authenticate_client(request, services)
        raise RelayError(404, "not_found", "Not found.", error_type="invalid_request_error")


def _authenticate_client(request: Request, services: Services) -> auth_mod.ClientKey:
    token = api_proxy.bearer(request)
    if token is None:
        from .errors import invalid_api_key

        raise invalid_api_key()
    if not token.startswith("gw_"):
        # 明确拒绝 tel_/wkr_/admin cookie 等跨角色凭证。
        from .errors import forbidden_role

        raise forbidden_role()
    client_key = auth_mod.lookup_client_key(services.db, services.pepper, token)
    if client_key is None:
        from .errors import invalid_api_key
        known = services.db.query_one("SELECT key_id FROM client_keys WHERE digest=?", (services.pepper.digest(token),))
        if known:
            context(request)["client_fp"] = services.pepper.digest("diagnostic-client\0"+known["key_id"])[:32]
        raise invalid_api_key()
    context(request)["client_fp"] = services.pepper.digest("diagnostic-client\0"+client_key.key_id)[:32]
    from .key_wallet import require_available
    require_available(services.db, client_key.key_id)
    return client_key


# --- 最小 liveness ------------------------------------------------------


def _register_health(app: FastAPI, services: Services) -> None:
    @app.get("/healthz")
    async def healthz() -> Response:
        # 仅最小 liveness：不含设备、版本、用量或密钥信息。
        return JSONResponse(content={"status": "ok"})


# --- 静态网站 -----------------------------------------------------------


def _register_web(app: FastAPI) -> None:
    def _index_response() -> Response:
        index_file = WEB_DIST / "index.html"
        if not index_file.is_file():
            return JSONResponse(
                status_code=503,
                content={
                    "error": {
                        "message": "Admin web assets are not built.",
                        "type": "server_error",
                        "code": "web_not_built",
                    }
                },
            )
        # 页面本体是公开的静态代码；私有数据一律经 /admin/api/* 鉴权取得。
        return FileResponse(index_file, headers={"Cache-Control": "no-store"})

    async def index() -> Response:
        return _index_response()

    app.add_api_route("/", index, methods=["GET"], name="web-index")

    # 同源多页路由：由前端脚本按 pathname 渲染对应视图。
    def _make_handler() -> Any:
        async def _handler() -> Response:
            return _index_response()

        return _handler

    for route in ("/quota", "/requests", "/settings", "/keys"):
        app.add_api_route(route, _make_handler(), methods=["GET"], name=f"web{route}")

    @app.get("/assets/{path:path}")
    async def assets(path: str) -> Response:
        # 只允许读 WEB_DIST 下的文件，禁止路径穿透。
        candidate = (WEB_DIST / "assets" / path).resolve()
        root = (WEB_DIST / "assets").resolve()
        if root not in candidate.parents and candidate != root:
            raise RelayError(404, "not_found", "Not found.", error_type="invalid_request_error")
        if not candidate.is_file():
            raise RelayError(404, "not_found", "Not found.", error_type="invalid_request_error")
        return FileResponse(candidate, headers={"Cache-Control": "no-cache"})
