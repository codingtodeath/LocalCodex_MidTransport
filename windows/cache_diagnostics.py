"""User-authorized local input capture; exactly 20 recent requests, no auth headers."""
import asyncio
import hashlib
import hmac
import json
import os
import re
import secrets
import threading
import time
import uuid
from collections import Counter
from datetime import datetime, timezone

import anyio
import httpx

from .routing import routing_headers
from .sse import SSEFramer

LIMIT = 20
MAX_FILE = 256 * 1024
INPUT_FIELDS = frozenset({"model", "instructions", "input", "tools", "tool_choice", "parallel_tool_calls",
    "reasoning", "text", "service_tier", "prompt_cache_key", "prompt_cache_options", "prompt_cache_retention",
    "previous_response_id", "include", "stream", "store", "metadata", "max_output_tokens", "temperature", "top_p"})


def now():
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z")


class CacheDiagnostics:
    def __init__(self, config):
        self.path = config.runtime_dir.parent / "logs/cache-recent.json"
        self.key_path = config.runtime_dir.parent / "secrets/cache-log-hmac.key"
        self.input_dir = self.path.parent / "cache-inputs"
        self.allowed_models = set(config.models_allowlist)
        self.capture_inputs = config.adapter.cache_diagnostics_capture_inputs
        self.lock = threading.Lock()
        self.rows = []
        self.key = None
        self.write_error = False
        try:
            self.path.parent.mkdir(exist_ok=True)
            self.input_dir.mkdir(exist_ok=True)
            try:
                fd = os.open(self.key_path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
            except FileExistsError:
                pass
            else:
                with os.fdopen(fd, "wb") as f:
                    f.write(secrets.token_bytes(32))
            with self.key_path.open("rb") as f:
                self.key = f.read(33)
            if len(self.key) != 32:
                self.key = None
                return
            if self.path.exists():
                with self.path.open("rb") as f:
                    raw = f.read(MAX_FILE + 1)
                if len(raw) <= MAX_FILE:
                    data = json.loads(raw)
                    if data.get("version") == 1 and isinstance(data.get("requests"), list):
                        self.rows = [r for r in data["requests"][-LIMIT:]
                                     if isinstance(r, dict) and r.get("schema_version") == 1]
            for record in self.rows:
                if record.get("transport_outcome") == "pending":
                    record["transport_outcome"] = "interrupted_by_restart"
            # Also create an empty ring now; startup does not call a model.
            self._save()
            self._prune()
        except Exception:
            self.write_error = True

    def digest(self, value):
        if value is None or self.key is None:
            return None
        raw = json.dumps(value, ensure_ascii=False, separators=(",", ":")).encode()
        return hmac.new(self.key, raw, hashlib.sha256).hexdigest()[:32]

    def _save(self):
        raw = json.dumps({"version": 1, "limit": LIMIT, "requests": self.rows},
                         ensure_ascii=True, separators=(",", ":")).encode()
        if len(raw) > MAX_FILE:
            raise ValueError("cache_log_too_large")
        temp = self.path.with_suffix(".tmp")
        with temp.open("wb") as f:
            f.write(raw)
        os.replace(temp, self.path)
        self.write_error = False

    def _prune(self):
        keep = {r.get("request_file") for r in self.rows}
        for p in self.input_dir.glob("*.request.json"):
            if re.fullmatch(r"[0-9a-f]{32}\.request\.json", p.name) and p.name not in keep:
                # Only fixed-format files directly inside this owned log directory.
                if p.resolve().parent == self.input_dir.resolve():
                    p.unlink()
        for p in self.input_dir.glob("*.request.tmp"):
            if re.fullmatch(r"[0-9a-f]{32}\.request\.tmp", p.name) and p.resolve().parent == self.input_dir.resolve():
                p.unlink()

    async def begin(self, payload, request_headers=None, account_id=None, client_stream=False):
        try:
            return await asyncio.to_thread(self._begin, payload, request_headers, account_id, client_stream)
        except Exception:
            self.write_error = True
            return None

    def _begin(self, payload, headers, account_id, client_stream):
        if self.key is None:
            return None
        routing = routing_headers(headers)
        anchor = routing.get("session-id") or routing.get("thread-id") or payload.get("prompt_cache_key")
        session = self.digest(anchor)
        account = self.digest(account_id)
        items = payload.get("input", [])
        items = items if isinstance(items, list) else [items]
        parameters = {k: payload.get(k) for k in ("model", "instructions", "tools", "tool_choice",
                      "parallel_tool_calls", "reasoning", "text", "service_tier",
                      "prompt_cache_options", "prompt_cache_retention")}
        with self.lock:
            previous = next((r for r in reversed(self.rows) if session is not None and
                             r.get("session_fp") == session and r.get("account_fp") == account), None)
            old_count = previous.get("input_item_count") if previous else None
            prefix = hmac.new(self.key, digestmod=hashlib.sha256)
            previous_prefix = prefix.hexdigest()[:32] if old_count == 0 else None
            sample = []
            roles = Counter()
            breakpoints = 0
            for i, item in enumerate(items):
                encoded = json.dumps(item, ensure_ascii=False, separators=(",", ":")).encode()
                prefix.update(len(encoded).to_bytes(8, "big"))
                prefix.update(encoded)
                if i < 32 or i >= len(items) - 8:
                    sample.append({"index": i, "fp": hmac.new(self.key, encoded, hashlib.sha256).hexdigest()[:32]})
                if i + 1 == old_count:
                    previous_prefix = prefix.hexdigest()[:32]
                if isinstance(item, dict):
                    kind = item.get("type") or item.get("role")
                    roles[kind if kind in {"user", "assistant", "developer", "system", "message",
                        "reasoning", "function_call", "function_call_output", "custom_tool_call",
                        "custom_tool_call_output", "configuration_update", "compaction"} else "other"] += 1
                    content = item.get("content")
                    if isinstance(content, list):
                        breakpoints += sum(isinstance(x, dict) and "prompt_cache_breakpoint" in x for x in content)
            fingerprints = {k + "_fp": self.digest(v) for k, v in parameters.items()}
            changed = [k[:-3] for k, v in fingerprints.items() if previous and previous.get(k) != v]
            request_id = None
            try:
                request_id = str(uuid.UUID(headers.get("x-relay-request-id", ""))) if headers else None
            except (ValueError, AttributeError):
                pass
            record_id = uuid.uuid4().hex
            request_file = None
            request_bytes = None
            # Known model request fields only. Never capture inbound/outbound headers
            # or auth-file content. Input/tool content is intentionally kept intact.
            if self.capture_inputs:
                request_file = record_id + ".request.json"
                captured = {k: v for k, v in payload.items() if k in INPUT_FIELDS}
                raw = json.dumps(captured, ensure_ascii=False, separators=(",", ":")).encode()
                request_bytes = len(raw)
                target = self.input_dir / request_file
                temp = target.with_suffix(".tmp")
                with temp.open("wb") as f:
                    f.write(raw)
                os.replace(temp, target)
            record = {
                "schema_version": 1, "id": record_id, "relay_request_id": request_id,
                "request_file": request_file, "request_bytes": request_bytes,
                "started_at": now(), "ended_at": None, "model": payload.get("model") if
                payload.get("model") in self.allowed_models else "unknown", "stream": client_stream is True,
                "session_fp": session, "account_fp": account,
                "session_id_fp": self.digest(routing.get("session-id")),
                "thread_id_fp": self.digest(routing.get("thread-id")),
                "cache_key_fp": self.digest(payload.get("prompt_cache_key")),
                "request_turn_state_fp": self.digest(routing.get("x-codex-turn-state")),
                "response_turn_state_fp": None, "previous_response_id_present": bool(payload.get("previous_response_id")),
                "input_item_count": len(items), "input_roles": dict(roles),
                "input_fp": prefix.hexdigest()[:32], "item_fingerprints": sample,
                "explicit_breakpoint_count": breakpoints, "changed_parameters": changed,
                "previous_record_id": previous.get("id") if previous else None,
                "previous_input_is_prefix": previous_prefix == previous.get("input_fp") if previous else None,
                "input_item_count_decreased": len(items) < old_count if type(old_count) is int else None,
                "http_status": None, "transport_outcome": "pending", "terminal_kind": None,
                "usage_status": "missing", "input_tokens": None, "cached_input_tokens": None,
                "output_tokens": None, "cache_hit_percent": None,
                **fingerprints,
            }
            self.rows.append(record)
            self.rows = self.rows[-LIMIT:]
            try:
                self._save()
            finally:
                self._prune()
            return record["id"]

    async def update(self, record_id, **fields):
        if record_id is None:
            return
        try:
            await asyncio.to_thread(self._update, record_id, fields)
        except Exception:
            self.write_error = True

    def _update(self, record_id, fields):
        with self.lock:
            record = next((r for r in self.rows if r["id"] == record_id), None)
            if record is not None:
                record.update(fields)
                self._save()


class DiagnosticStream(httpx.AsyncByteStream):
    def __init__(self, source, log, record_id, status, started, diagnostics=None, request_id=None):
        self.source, self.log, self.record_id = source, log, record_id
        self.status, self.started = status, started
        self.diagnostics = diagnostics
        self.request_id = request_id
        self.last_byte_ms = None
        self.bytes_received = self.chunks_received = 0
        self.parser = SSEFramer()
        self.parse_error = False
        self.first_byte_ms = None
        self.finished = False

    async def __aiter__(self):
        outcome = "interrupted"
        try:
            async for chunk in self.source:
                self.last_byte_ms = round((time.monotonic()-self.started)*1000)
                self.bytes_received += len(chunk)
                self.chunks_received += 1
                if self.first_byte_ms is None:
                    self.first_byte_ms = round((time.monotonic() - self.started) * 1000)
                if not self.parse_error:
                    try:
                        self.parser.feed(chunk)
                    except Exception:
                        self.parse_error = True
                        self.parser = SSEFramer()
                yield chunk
            outcome = "eof"
        except BaseException as exc:
            import asyncio
            if isinstance(exc, asyncio.CancelledError): outcome = "cancelled_cause_unknown"
            elif isinstance(exc, GeneratorExit): outcome = "closed_before_eof"
            if self.diagnostics:
                from .runtime_diagnostics import exception_info
                self.diagnostics.emit("inference_stream_failed", request_id=self.request_id, cache_record_id=self.record_id,
                    **exception_info(exc), last_byte_ms=self.last_byte_ms,
                    duration_ms=round((time.monotonic()-self.started)*1000))
            raise
        finally:
            await self.finish(outcome)

    async def finish(self, outcome):
        if self.finished:
            return
        self.finished = True
        terminal = self.parser.terminal
        if self.diagnostics:
            self.diagnostics.emit("inference_stream_finished", request_id=self.request_id, cache_record_id=self.record_id,
                outcome=outcome, http_status=self.status, terminal_seen=terminal is not None,
                terminal_kind=terminal[0] if terminal else None, parse_error=self.parse_error or bool(self.parser.buffer),
                terminal_conflict=self.parser.conflict,
                first_byte_ms=self.first_byte_ms, last_byte_ms=self.last_byte_ms,
                bytes_received=self.bytes_received, chunks_received=self.chunks_received,
                duration_ms=round((time.monotonic()-self.started)*1000))
        usage = terminal[2] if terminal and isinstance(terminal[2], dict) else {}
        detail = usage.get("input_tokens_details")
        detail = detail if isinstance(detail, dict) else {}
        def token(value):
            return value if type(value) is int and 0 <= value <= 2**63-1 else None
        inp, cached, out = token(usage.get("input_tokens")), token(detail.get("cached_tokens")), token(usage.get("output_tokens"))
        status = "reported" if inp is not None and cached is not None and out is not None else "missing"
        if cached is not None and inp is not None and cached > inp:
            status = "invalid"
        if self.parser.conflict:
            status = "conflict"
        if self.parse_error or self.parser.buffer:
            status = "incomplete"
        with anyio.move_on_after(3, shield=True):
            await self.log.update(self.record_id, ended_at=now(), transport_outcome=outcome,
                terminal_kind=terminal[0] if terminal and terminal[0] in {
                    "response.completed", "response.failed", "response.incomplete"} else None,
                usage_status=status, input_tokens=inp, cached_input_tokens=cached, output_tokens=out,
                cache_hit_percent=round(100 * cached / inp, 2) if status == "reported" and inp else None,
                first_byte_ms=self.first_byte_ms, duration_ms=round((time.monotonic()-self.started)*1000))

    async def aclose(self):
        try:
            await self.source.aclose()
        finally:
            await self.finish("closed_before_eof")
