"""Structured connection diagnostics, without HTTP bodies, credentials or raw errors."""
import asyncio
import json
import math
import os
import re
import threading
import time
import uuid
from datetime import datetime, timezone

import httpx

EVENTS = {"request_started", "request_finished", "request_rejected", "request_failed", "worker_opened", "stream_finished"}
CODES = {"invalid_api_key", "forbidden_role", "not_found", "request_failed", "internal_error",
    "insufficient_balance", "balance_metering_unavailable",
    "upstream_connect_failed", "upstream_unavailable", "upstream_overloaded", "upstream_quota_exhausted",
    "invalid_request", "model_not_allowed", "gateway_concurrency_limit", "worker_unavailable",
    "adapter_unavailable", "upstream_reauth_required", "upstream_rate_limited", "upstream_timeout",
    "upstream_protocol_error", "upstream_request_rejected", "metering_unavailable", "payload_too_large",
    "unsupported_media_type", "client_disconnected", "request_deadline_exceeded", "upstream_stream_interrupted"}
OUTCOMES = {"completed", "response_complete", "response_failed", "response_incomplete", "no_terminal",
    "key_balance_exhausted", "key_disabled",
    "cancelled_cause_unknown", "stream_closed", "worker_idle_timeout", "worker_connect_timeout", "worker_write_timeout",
    "worker_pool_timeout", "worker_connect_error", "worker_protocol_error", "worker_read_error", "request_deadline_exceeded",
    "protocol_invalid", "http_error", "unexpected_error", "stream_not_started"}
NUMBERS = {"http_status", "duration_ms", "headers_ms", "first_byte_ms", "last_byte_ms", "bytes_sent",
    "bytes_received", "chunks_received", "global_active", "client_active", "global_limit", "client_limit", "heartbeat_age_seconds"}
FLAGS = {"response_complete", "disconnect_observed", "terminal_seen", "terminal_conflict", "heartbeat_fresh", "probe_fresh",
    "instance_consistent", "adapter_ready", "bridge_ready"}
HEX = re.compile(r"[0-9a-f]{32}\Z")


def error_kind(exc):
    for kind, label in ((asyncio.CancelledError, "cancelled_cause_unknown"), (GeneratorExit, "stream_closed"),
        (httpx.ConnectTimeout, "worker_connect_timeout"), (httpx.ReadTimeout, "worker_idle_timeout"),
        (httpx.WriteTimeout, "worker_write_timeout"), (httpx.PoolTimeout, "worker_pool_timeout"),
        (TimeoutError, "request_deadline_exceeded"), (httpx.ConnectError, "worker_connect_error"),
        (httpx.ProtocolError, "worker_protocol_error"), (httpx.HTTPError, "worker_read_error"),
        (ValueError, "protocol_invalid")):
        if isinstance(exc, kind): return label
    return "unexpected_error"


def context(request):
    return request.scope.setdefault("state", {}).setdefault("connection_context", {})


class ConnectionLog:
    def __init__(self, path):
        self.path = path
        self.lock = threading.Lock()
        self.write_error = False

    def emit(self, event, **fields):
        if event not in EVENTS: return
        try:
            r = {"version": 1, "at": datetime.now(timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z"), "event": event}
            for k, v in fields.items():
                if k in NUMBERS and type(v) in (int, float) and abs(v) < 2**63 and math.isfinite(v): r[k] = round(v, 3)
                elif k in FLAGS and type(v) is bool: r[k] = v
                elif k in {"request_id"} and isinstance(v, str):
                    try: r[k] = str(uuid.UUID(v))
                    except ValueError: pass
                elif k in {"edge_id", "client_fp", "session_fp", "thread_fp"} and isinstance(v, str) and HEX.fullmatch(v): r[k] = v
                elif k == "error_code" and isinstance(v, str): r[k] = v if v in CODES else "other"
                elif k == "outcome" and v in OUTCOMES: r[k] = v
                elif k == "method" and v in {"GET", "POST", "PUT", "PATCH", "DELETE", "HEAD", "OPTIONS"}: r[k] = v
                elif k == "path" and v in {"/v1/models", "/v1/responses", "/v1/other"}: r[k] = v
                elif k == "worker_state" and v in {"unknown", "online", "degraded", "offline"}: r[k] = v
                elif k == "auth_state" and v in {"unknown", "ready", "reauth_required"}: r[k] = v
            raw = (json.dumps(r, allow_nan=False) + "\n").encode()
            with self.lock:
                self.path.parent.mkdir(parents=True, exist_ok=True)
                if self.path.exists() and self.path.stat().st_size + len(raw) > 2*1024*1024:
                    for i in range(3, 0, -1):
                        source = self.path if i == 1 else self.path.with_name(f"gateway-events.{i-1}.jsonl")
                        if source.exists(): os.replace(source, self.path.with_name(f"gateway-events.{i}.jsonl"))
                with os.fdopen(os.open(self.path, os.O_WRONLY|os.O_APPEND|os.O_CREAT, 0o600), "ab") as f: f.write(raw)
            self.write_error = False
        except (OSError, ValueError, TypeError, OverflowError): self.write_error = True


class ConnectionObserver:
    """Pure ASGI observer: no response buffering and no extra reads of request data."""
    def __init__(self, app, services):
        self.app, self.services = app, services

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http" or not (scope.get("path") == "/v1" or scope.get("path", "").startswith("/v1/")):
            return await self.app(scope, receive, send)
        started = time.monotonic()
        fields = {"request_id": str(uuid.uuid4()), "method": scope["method"], "path": scope["path"] if scope["path"] in {"/v1/models", "/v1/responses"} else "/v1/other"}
        headers = {k.lower(): v for k, v in scope.get("headers", [])}
        peer = (scope.get("client") or (None,))[0]
        edge = headers.get(b"x-relay-edge-id", b"").decode("ascii", errors="ignore")
        if peer in self.services.config.trusted_proxy_hosts and HEX.fullmatch(edge): fields["edge_id"] = edge
        for header, key in ((b"session-id", "session_fp"), (b"thread-id", "thread_fp")):
            value = headers.get(header)
            if value and len(value) <= 8192:
                fields[key] = self.services.pepper.digest("diagnostic-"+key+"\0"+value.decode("utf-8", errors="replace"))[:32]
        scope.setdefault("state", {})["connection_context"] = fields
        log = self.services.connection_log
        log.emit("request_started", **fields)
        finished = False
        async def recv():
            message = await receive()
            if message["type"] == "http.disconnect": fields["disconnect_observed"] = True
            return message
        async def output(message):
            nonlocal finished
            if message["type"] == "http.response.start":
                fields["http_status"] = message["status"]
                fields["headers_ms"] = (time.monotonic()-started)*1000
                message = dict(message)
                message["headers"] = [(k,v) for k,v in message.get("headers",[]) if k.lower() != b"x-request-id"] + [(b"x-request-id",fields["request_id"].encode())]
            elif message["type"] == "http.response.body":
                size = len(message.get("body", b""))
                if size:
                    fields.setdefault("first_byte_ms", (time.monotonic()-started)*1000)
                    fields["last_byte_ms"] = (time.monotonic()-started)*1000
                fields["bytes_sent"] = fields.get("bytes_sent",0) + size
            await send(message)
            if message["type"] == "http.response.body" and not message.get("more_body",False): finished = True
        try:
            await self.app(scope, recv, output)
        except BaseException as exc:
            fields.setdefault("outcome", error_kind(exc))
            raise
        finally:
            fields["response_complete"] = finished
            fields["duration_ms"] = (time.monotonic()-started)*1000
            event = "request_rejected" if fields.get("http_status",0) >= 400 else "request_finished" if finished else "request_failed"
            log.emit(event, **fields)
