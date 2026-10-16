"""Mock Windows Bridge。

用途：在没有真实 Windows / 订阅账号的情况下开发并联调 Ubuntu 侧
（见 01_DEVELOPMENT_SPEC.md 的 M1 里程碑）。它只实现 Bridge 的三种白名单
路由，绝不含任何真实凭证或真实推理能力。

安全提醒：这是测试替身。生产部署不应运行它，生产环境也拒绝
`enable_mock=true`。

实现要点：
* 仅 `GET /v1/models`、`POST /v1/responses`、`GET /internal/v1/health`。
* `wkr_...` Bearer 认证。
* 其余路径 404，management / auth-files / app-server 一律不可达。
* `/v1/responses` 支持 stream=true 的 SSE 与 stream=false 的完整 JSON。
"""

from __future__ import annotations

import asyncio
import json
import time
import uuid
from typing import Any, AsyncIterator

from fastapi import FastAPI, Request, Response
from fastapi.responses import JSONResponse, StreamingResponse

MOCK_MODELS = ("fixture-text-v1", "fixture-text-small-v1")

_comment = ": synthetic mock bridge, not live usage"


def _usage(input_tokens: int, cached: int, output_tokens: int, reasoning: int) -> dict[str, Any]:
    return {
        "input_tokens": input_tokens,
        "input_tokens_details": {"cached_tokens": cached, "cache_write_tokens": 0},
        "output_tokens": output_tokens,
        "output_tokens_details": {"reasoning_tokens": reasoning},
        "total_tokens": input_tokens + output_tokens,
    }


def _sse(event: str, payload: dict[str, Any]) -> bytes:
    return f"event: {event}\ndata: {json.dumps(payload, ensure_ascii=False)}\n\n".encode("utf-8")


async def _sse_stream(
    response_id: str,
    model: str,
    *,
    chunks: list[str] | None = None,
    chunk_delay: float = 0.01,
    emit_usage: bool = True,
    duplicate_terminal: bool = False,
    truncate: bool = False,
    usage: dict[str, Any] | None = None,
) -> AsyncIterator[bytes]:
    """合成 SSE 流。

    `truncate=True` 用于模拟“已发 200 后断流”：不产出终态事件。
    """

    yield _comment.encode("utf-8") + b"\n"

    text = chunks or ["你好，", "这是 Mock Bridge 的合成流。"]
    yield _sse(
        "response.created",
        {
            "type": "response.created",
            "sequence_number": 0,
            "response": {"id": response_id, "status": "in_progress", "model": model, "usage": None},
        },
    )
    seq = 1
    for piece in text:
        yield _sse(
            "response.output_text.delta",
            {
                "type": "response.output_text.delta",
                "sequence_number": seq,
                "item_id": "msg_mock_001",
                "output_index": 0,
                "content_index": 0,
                "delta": piece,
            },
        )
        seq += 1
        if chunk_delay:
            await asyncio.sleep(chunk_delay)

    if truncate:
        # 断开连接，不发送终态。
        return

    usage = usage or _usage(10000, 4000, 1000, 300)
    terminal = {
        "type": "response.completed",
        "sequence_number": seq,
        "response": {
            "id": response_id,
            "object": "response",
            "status": "completed",
            "model": model,
            "usage": usage if emit_usage else None,
        },
    }
    yield _sse("response.completed", terminal)
    if duplicate_terminal:
        yield _sse("response.completed", terminal)


def create_mock_bridge(worker_key: str, worker_id: str = "home-windows") -> FastAPI:
    app = FastAPI(title="Mock Windows Bridge", docs_url=None, redoc_url=None, openapi_url=None)
    instance_id = str(uuid.uuid4())
    state: dict[str, Any] = {
        "adapter_ready": True,
        "auth_state": "ready",
        "adapter_status": "running",
        "active_requests": 0,
        "last_inference_success_at": None,
        "stream_mode": "ok",
        "request_count": 0,
        "sse_chunks": None,
        "chunk_delay": 0.01,
        "usage": None,
    }
    app.state.mock = state
    app.state.instance_id = instance_id

    def authorized(request: Request) -> bool:
        header = request.headers.get("authorization") or ""
        if not header.lower().startswith("bearer "):
            return False
        import hmac

        return hmac.compare_digest(header[7:].strip(), worker_key)

    def unauthorized() -> JSONResponse:
        return JSONResponse(
            status_code=401,
            content={"error": {"message": "Unauthorized.", "type": "invalid_request_error", "code": "invalid_api_key"}},
        )

    @app.get("/v1/models")
    async def models(request: Request) -> Response:
        if not authorized(request):
            return unauthorized()
        return JSONResponse(
            content={
                "object": "list",
                "data": [
                    {"id": m, "object": "model", "created": 0, "owned_by": "mock"}
                    for m in MOCK_MODELS
                ],
            }
        )

    @app.get("/internal/v1/health")
    async def health(request: Request) -> Response:
        if not authorized(request):
            return unauthorized()
        from datetime import UTC, datetime

        return JSONResponse(
            content={
                "protocol_version": "1.0",
                "worker_id": worker_id,
                "instance_id": app.state.instance_id,
                "observed_at": datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%S.%f")[:-3] + "Z",
                "local": {
                    "bridge_ready": True,
                    "adapter_status": state["adapter_status"],
                    "adapter_ready": state["adapter_ready"],
                    "auth_state": state["auth_state"],
                    "active_requests": state["active_requests"],
                    "last_inference_success_at": state["last_inference_success_at"],
                },
            }
        )

    @app.post("/v1/responses")
    async def responses(request: Request) -> Response:
        if not authorized(request):
            return unauthorized()
        try:
            body = await request.json()
        except Exception:
            return JSONResponse(status_code=400, content={"error": {"message": "bad json"}})
        if not isinstance(body, dict):
            return JSONResponse(status_code=400, content={"error": {"message": "bad body"}})

        model = body.get("model", "fixture-text-v1")
        stream = bool(body.get("stream"))
        state["request_count"] += 1
        response_id = f"resp_mock_{uuid.uuid4().hex[:12]}"

        if not state["adapter_ready"]:
            return JSONResponse(
                status_code=503,
                content={"error": {"message": "adapter not ready", "code": "adapter_unavailable"}},
            )
        if state["auth_state"] == "reauth_required":
            return JSONResponse(
                status_code=401,
                content={"error": {"message": "reauth required", "code": "upstream_reauth_required"}},
            )
        mode = state["stream_mode"]
        if mode == "rate_limited":
            return JSONResponse(
                status_code=429,
                headers={"Retry-After": "7"},
                content={"error": {"message": "rate limited", "code": "rate_limit_exceeded"}},
            )
        if mode == "redirect":
            return JSONResponse(
                status_code=307,
                headers={"Location": "https://example.invalid/steal"},
                content={"error": {"message": "redirect"}},
            )
        if mode == "invalid_json":
            return Response(content=b"<html>not json</html>", media_type="application/json")
        if mode == "server_error":
            return JSONResponse(status_code=500, content={"error": {"message": "boom"}})
        if mode == "slow_headers":
            await asyncio.sleep(float(body.get("__mock_delay_seconds", 6)))

        if not stream:
            return JSONResponse(
                content={
                    "id": response_id,
                    "object": "response",
                    "status": "completed",
                    "model": model,
                    "output": [],
                    "usage": state["usage"] or _usage(10000, 4000, 1000, 300),
                }
            )

        # 允许测试通过请求体调整合成流的长度与节奏（仅 Mock 支持）。
        chunk_count = body.get("__mock_chunk_count")
        chunk_delay_ms = body.get("__mock_chunk_delay_ms")
        chunks = state["sse_chunks"]
        if isinstance(chunk_count, int) and chunk_count > 0:
            chunks = [f"合成分块 {index}。" for index in range(min(chunk_count, 200))]
        if isinstance(chunk_delay_ms, (int, float)) and chunk_delay_ms >= 0:
            payload_delay = min(float(chunk_delay_ms), 1000.0) / 1000.0
        else:
            payload_delay = state["chunk_delay"]

        generator = _sse_stream(
            response_id,
            model,
            chunks=chunks,
            chunk_delay=payload_delay,
            duplicate_terminal=mode == "duplicate_terminal",
            truncate=mode == "truncate",
            usage=state["usage"],
        )
        return StreamingResponse(generator, media_type="text/event-stream")

    # --- Mock 专用调试开关（只在本测试替身中存在；生产 Bridge 没有此端点） ---
    @app.post("/__mock/state")
    async def set_state(request: Request) -> Response:
        try:
            patch = await request.json()
        except Exception:
            return JSONResponse(status_code=400, content={"error": {"message": "bad json"}})
        if not isinstance(patch, dict):
            return JSONResponse(status_code=400, content={"error": {"message": "bad body"}})
        allowed = {
            "adapter_ready",
            "auth_state",
            "adapter_status",
            "stream_mode",
            "chunk_delay",
            "sse_chunks",
            "usage",
            "request_count",
        }
        unknown = sorted(set(patch) - allowed)
        if unknown:
            return JSONResponse(
                status_code=400,
                content={"error": {"message": f"unknown mock keys: {unknown}"}},
            )
        state.update(patch)
        return JSONResponse(content={"ok": True, "state": {k: state[k] for k in sorted(allowed)}})

    @app.get("/__mock/state")
    async def get_state() -> Response:
        return JSONResponse(
            content={
                "adapter_ready": state["adapter_ready"],
                "auth_state": state["auth_state"],
                "stream_mode": state["stream_mode"],
                "request_count": state["request_count"],
                "instance_id": app.state.instance_id,
            }
        )

    # 明确不存在：management / auth-files / app-server 等
    @app.api_route(
        "/{path:path}",
        methods=["GET", "POST", "PUT", "PATCH", "DELETE"],
    )
    async def everything_else(path: str) -> Response:
        return JSONResponse(status_code=404, content={"error": {"message": "Not found."}})

    return app
