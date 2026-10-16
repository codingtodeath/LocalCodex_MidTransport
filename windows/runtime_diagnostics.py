"""Bounded local event log. Never serialize exceptions, credentials or HTTP data."""
import base64
import asyncio
import hashlib
import json
import math
import os
import ssl
import uuid
import re
import threading
import time
from datetime import datetime, timezone

import httpx

EVENTS = frozenset({"worker_started", "worker_stopping", "worker_stopped", "worker_failed",
    "worker_component_failed",
    "credential_observed", "credential_changed", "credential_expired", "credential_valid",
    "credentials_failed", "credentials_recovered", "openai_check_failed", "openai_check_recovered",
    "openai_check_healthy", "readiness_changed", "inference_open_failed", "inference_http_error",
    "inference_stream_failed", "tunnel_started", "tunnel_exited", "tunnel_start_failed",
    "tunnel_stopped", "telemetry_failed", "telemetry_recovered", "quota_failed", "quota_recovered",
    "inference_opened", "inference_stream_finished", "bridge_request_started", "bridge_request_finished", "bridge_stream_failed",
    "inference_attempt_started", "inference_attempt_failed", "inference_retry_scheduled", "inference_retry_exhausted"})
EVENTS = EVENTS | frozenset({"credential_refresh_started", "credential_refresh_finished", "credential_refresh_failed"})
EVENTS = EVENTS | frozenset({'response_assembly_failed'})
EVENTS = EVENTS | frozenset({'incident_captured','incident_capture_failed'})
CODES = frozenset({"ok", "timeout", "connect_timeout", "read_timeout", "write_timeout", "pool_timeout",
    "tls_error", "connect_error", "proxy_error", "protocol_error", "network_error", "auth_unavailable",
    "unauthorized", "forbidden", "rate_limited", "upstream_server_error", "http_error",
    "invalid_response", "auth_changed_during_check", "empty_catalog", "credential_changed",
    "probe_stale", "upstream_unavailable", "unexpected_error", "cancelled", "os_error", "body_too_large", "retry_budget_exhausted",
    "invalid_ack", "heartbeat_not_current", "invalid_command", "rejected", "quota_unavailable"})
CODES = CODES | frozenset({"gateway_concurrency_limit", "adapter_unavailable", "upstream_timeout",
    "upstream_rate_limited", "upstream_reauth_required", "upstream_protocol_error", "upstream_error",
    "invalid_api_key", "invalid_request", "invalid_json", "invalid_content_length", "request_too_large",
    "upstream_connect_failed", "upstream_unavailable", "upstream_overloaded", "upstream_quota_exhausted",
    "json_required", "compressed_body_not_supported", "model_not_allowed", "hosted_tools_not_supported"})
ERROR_TYPES = frozenset({"TimeoutError", "ConnectTimeout", "ReadTimeout", "WriteTimeout", "PoolTimeout",
    "ConnectError", "ProxyError", "RemoteProtocolError", "LocalProtocolError", "ReadError", "WriteError",
    "SSLError", "SSLCertVerificationError", "FileNotFoundError", "PermissionError", "OSError",
    "ValueError", "TypeError", "KeyError", "AttributeError", "JSONDecodeError", "ValidationError",
    "CancelledError", "RuntimeError"})
NUMBERS = frozenset({"duration_ms", "timeout_seconds", "remaining_seconds", "http_status",
    "process_id", "parent_process_id",
    "consecutive_failures", "outage_seconds", "exit_code", "retry_seconds", "seq", "failures",
    "first_byte_ms", "last_byte_ms", "bytes_received", "chunks_received", "bytes_sent", "active_requests", "max_concurrency",
    "attempt_no", "max_attempts", "retry_wait_ms", "retry_budget_ms"})
FLAGS = frozenset({"ready", "access_token_changed", "identity_changed", "refresh_metadata_changed",
    "terminal_seen", "parse_error", "terminal_conflict", "disconnect_observed", "response_complete", "retryable", "request_sent"})
TIMESTAMPS = frozenset({"expires_at", "previous_expires_at", "last_refresh", "previous_last_refresh",
    "auth_file_modified_at"})
TLS_REASONS = frozenset({"CERTIFICATE_VERIFY_FAILED", "UNEXPECTED_EOF_WHILE_READING",
    "WRONG_VERSION_NUMBER", "TLSV1_ALERT_INTERNAL_ERROR", "TLSV1_ALERT_ACCESS_DENIED",
    "SSLV3_ALERT_HANDSHAKE_FAILURE"})


def stamp(value=None):
    return (value or datetime.now(timezone.utc)).astimezone(timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z")


def safe_time(value):
    try:
        d = datetime.fromisoformat(value.replace("Z", "+00:00"))
        return stamp(d) if d.tzinfo else None
    except (ValueError, TypeError, AttributeError, OverflowError):
        return None


def exception_info(exc):
    # Type/cause only; exception messages can contain URLs, headers or response bodies.
    # A cancelled TLS read may carry SSLWantReadError in its context. It is not
    # a certificate failure: preserve the outer timeout/cancellation first.
    if isinstance(exc, asyncio.CancelledError):
        return {"error_code": "cancelled", "error_type": "CancelledError"}
    for kind, label in ((httpx.ConnectTimeout, "connect_timeout"), (httpx.ReadTimeout, "read_timeout"),
            (httpx.WriteTimeout, "write_timeout"), (httpx.PoolTimeout, "pool_timeout"), (TimeoutError, "timeout")):
        if isinstance(exc, kind):
            return {"error_code": label, "error_type": type(exc).__name__ if type(exc).__name__ in ERROR_TYPES else "TimeoutError"}
    current = exc
    for _ in range(6):
        if isinstance(current, ssl.SSLError) and not isinstance(current, (ssl.SSLWantReadError, ssl.SSLWantWriteError)):
            result = {"error_code": "tls_error", "error_type": "SSLError"}
            reason = getattr(current, "reason", None)
            if reason in TLS_REASONS:
                result["tls_reason"] = reason
            return result
        current = getattr(current, "__cause__", None) or getattr(current, "__context__", None)
        if current is None: break
    code = "unexpected_error"
    for kind, label in ((httpx.ConnectTimeout, "connect_timeout"), (httpx.ReadTimeout, "read_timeout"),
            (httpx.WriteTimeout, "write_timeout"), (httpx.PoolTimeout, "pool_timeout"),
            (TimeoutError, "timeout"), (httpx.ProxyError, "proxy_error"), (httpx.ConnectError, "connect_error"),
            (httpx.ProtocolError, "protocol_error"), (httpx.HTTPError, "network_error"),
            (OSError, "os_error"), (ValueError, "invalid_response"), (TypeError, "invalid_response")):
        if isinstance(exc, kind): code = label; break
    return {"error_code": code, "error_type": type(exc).__name__ if type(exc).__name__ in ERROR_TYPES else "RuntimeError"}


def http_code(status):
    return {401: "unauthorized", 403: "forbidden", 429: "rate_limited"}.get(status,
        "upstream_server_error" if status >= 500 else "http_error")


class RuntimeDiagnostics:
    def __init__(self, config, *, max_bytes=1024*1024, backups=3):
        self.path = config.runtime_dir.parent / "logs/runtime-events.jsonl"
        self.max_bytes, self.backups = max_bytes, backups
        self.lock = threading.Lock()
        self.write_error = False
        self.failures = {}
        self.success_at = {}
        self.previous_auth = None
        self.expired = None
        self.previous_readiness = None

    def emit(self, event, **fields):
        if event not in EVENTS: return
        record = {"version": 1, "at": stamp(), "event": event}
        for k, v in fields.items():
            if k in NUMBERS and type(v) in (int, float) and math.isfinite(v): record[k] = v
            elif k in FLAGS and type(v) is bool: record[k] = v
            elif k in TIMESTAMPS: record[k] = safe_time(v)
            elif k == "error_code" and isinstance(v, str) and v in CODES: record[k] = v
            elif k == "error_type" and isinstance(v, str) and v in ERROR_TYPES: record[k] = v
            elif k == "tls_reason" and v in TLS_REASONS: record[k] = v
            elif k == "request_id" and isinstance(v, str):
                try: record[k] = str(uuid.UUID(v))
                except ValueError: pass
            elif k == "instance_id" and isinstance(v, str):
                try: record[k] = str(uuid.UUID(v))
                except ValueError: pass
            elif k == "cache_record_id" and isinstance(v,str) and re.fullmatch(r"[0-9a-f]{32}",v): record[k] = v
            elif k == 'incident_id' and isinstance(v,str) and re.fullmatch(r'\d{20}-[0-9a-f]{32}',v):record[k]=v
            elif k == "outcome" and v in {"eof", "interrupted", "closed_before_eof", "cancelled_cause_unknown"}: record[k] = v
            elif k == "terminal_kind" and v in {"response.completed", "response.failed", "response.incomplete"}: record[k] = v
            elif k == "path" and v in {"/v1/models", "/v1/responses"}: record[k] = v
            elif k == "stage" and v in {"before_send", "awaiting_response", "received_error"}: record[k] = v
            elif k == 'stage' and v=='nonstream_assembly':record[k]=v
            elif k=='protocol_failure' and v in {'response_too_large','conflicting_terminal','conflicting_output_item','upstream_error',
                'missing_terminal','conflicting_output_text','invalid_output_item','invalid_output_content',
                'missing_output_content_part','invalid_terminal','invalid_terminal_output','event_after_terminal'}:record[k]=v
            elif k == "component" and v in {"bridge_server", "control_loop", "quota_loop", "telemetry_loop"}: record[k] = v
        try:
            line = (json.dumps(record, ensure_ascii=True, allow_nan=False) + "\n").encode()
            with self.lock:
                self.path.parent.mkdir(parents=True, exist_ok=True)
                if self.path.exists() and self.path.stat().st_size + len(line) > self.max_bytes:
                    for i in range(self.backups, 0, -1):
                        source = self.path if i == 1 else self.path.with_name(f"runtime-events.{i-1}.jsonl")
                        target = self.path.with_name(f"runtime-events.{i}.jsonl")
                        if source.exists(): os.replace(source, target)
                fd = os.open(self.path, os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600)
                with os.fdopen(fd, "ab") as f: f.write(line)
            self.write_error = False
        except (OSError, ValueError, TypeError):
            self.write_error = True  # Logging failure must not stop relay service.

    def check(self, component, ok, **fields):
        if component not in {"openai_check", "credentials", "telemetry", "quota"}: return
        now = time.monotonic()
        if not ok:
            count, first = self.failures.get(component, (0, now))
            self.failures[component] = (count + 1, first)
            self.emit(component + "_failed", consecutive_failures=count+1, **fields)
        elif component in self.failures:
            count, first = self.failures.pop(component)
            self.emit(component + "_recovered", failures=count, outage_seconds=round(now-first, 3), **fields)
            self.success_at[component] = now
        elif component == "openai_check" and now - self.success_at.get(component, -math.inf) >= 300:
            self.emit("openai_check_healthy", **fields)
            self.success_at[component] = now

    def readiness(self, ready, code):
        if ready != self.previous_readiness:
            self.emit("readiness_changed", ready=ready, error_code=code)
            self.previous_readiness = ready

    def observe_auth(self, path):
        try:
            with path.open("rb") as f: raw = f.read(65537)
            if len(raw) > 65536: raise ValueError()
            d = json.loads(raw); tokens = d.get("tokens") or {}
            access, account = tokens.get("access_token"), tokens.get("account_id")
            if not isinstance(access, str) or not access or not isinstance(account, str) or not account:
                raise ValueError()
            p = access.split(".")[1]
            claims = json.loads(base64.urlsafe_b64decode(p + "="*(-len(p)%4)))
            exp = claims.get("exp")
            expiry = datetime.fromtimestamp(exp, timezone.utc) if type(exp) in (int, float) and math.isfinite(exp) else None
            # Fingerprints exist only in memory; never written to the event log.
            identity = str(claims.get("sub", "")) + "\0" + account
            meta = {"expires_at": stamp(expiry) if expiry else None, "last_refresh": safe_time(d.get("last_refresh")),
                "auth_file_modified_at": stamp(datetime.fromtimestamp(path.stat().st_mtime, timezone.utc))}
            current = (hashlib.sha256(access.encode()).digest(), hashlib.sha256(identity.encode()).digest(), meta)
            prior = self.previous_auth
            if prior is None: self.emit("credential_observed", **meta)
            elif current != prior:
                self.emit("credential_changed", **meta, previous_expires_at=prior[2]["expires_at"],
                    previous_last_refresh=prior[2]["last_refresh"], access_token_changed=current[0] != prior[0],
                    identity_changed=current[1] != prior[1], refresh_metadata_changed=meta["last_refresh"] != prior[2]["last_refresh"])
            self.previous_auth = current
            if expiry:
                remaining = (expiry-datetime.now(timezone.utc)).total_seconds()
                expired = remaining <= 0
                if expired != self.expired:
                    self.emit("credential_expired" if expired else "credential_valid", expires_at=meta["expires_at"], remaining_seconds=round(remaining))
                    self.expired = expired
            self.check("credentials", True)
        except (OSError, ValueError, TypeError, KeyError, IndexError, AttributeError, OverflowError) as exc:
            self.check("credentials", False, error_code="auth_unavailable", error_type=exception_info(exc)["error_type"])


def diagnostics(config, state):
    if getattr(state, "diagnostics", None) is None:
        state.diagnostics = RuntimeDiagnostics(config)
    return state.diagnostics


class BridgeObserver:
    def __init__(self, app, config, state):
        self.app, self.config, self.state = app, config, state
        self.log = diagnostics(config, state)

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http" or scope.get("path") not in {"/v1/models", "/v1/responses"}:
            return await self.app(scope, receive, send)
        started = time.monotonic();headers = {k.lower():v for k,v in scope.get("headers", [])}
        try: ident = str(uuid.UUID(headers.get(b"x-relay-request-id",b"").decode("ascii")))
        except (ValueError, UnicodeError): ident = str(uuid.uuid4())
        fields = {"request_id":ident, "path":scope["path"], "active_requests":self.state.active_requests,
            "max_concurrency":self.config.bridge.max_concurrency}
        self.log.emit("bridge_request_started", **fields)
        complete = False
        async def recv():
            message = await receive()
            if message["type"] == "http.disconnect": fields["disconnect_observed"] = True
            return message
        async def output(message):
            nonlocal complete
            if message["type"] == "http.response.start":
                fields["http_status"] = message["status"]
                for k,v in message.get("headers",[]):
                    if k.lower() == b"x-relay-error-code": fields["error_code"] = v.decode("ascii",errors="ignore")
            elif message["type"] == "http.response.body":
                size = len(message.get("body",b""))
                if size:
                    fields.setdefault("first_byte_ms",round((time.monotonic()-started)*1000))
                    fields["last_byte_ms"] = round((time.monotonic()-started)*1000)
                fields["bytes_sent"] = fields.get("bytes_sent",0)+size
            await send(message)
            if message["type"] == "http.response.body" and not message.get("more_body",False): complete = True
        try: await self.app(scope, recv, output)
        except BaseException as exc:
            fields.update(exception_info(exc))
            raise
        finally:
            fields.update(response_complete=complete,duration_ms=round((time.monotonic()-started)*1000))
            self.log.emit("bridge_request_finished", **fields)
