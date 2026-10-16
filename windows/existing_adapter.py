"""Fixed Codex HTTPS endpoints using the existing session as a read-only consumer."""
import asyncio
import hashlib
import json
import time

import httpx
import anyio

from .existing_auth import ExistingAuth
from .sse import SSEFramer
from .routing import routing_headers
from .cache_diagnostics import CacheDiagnostics, DiagnosticStream, now
from .runtime_diagnostics import diagnostics, exception_info, http_code
from .safe_retry import send_with_retry
from .proxy_transport import SafeProxyTransport
from .response_output import CompleteOutputStream
from .incident_capture import IncidentStore

BASE = "https://chatgpt.com/backend-api/codex"


class ExistingAdapter:
    def __init__(self, config, state, transport=None):
        self.config, self.state = config, state
        self.auth = ExistingAuth(config.adapter.existing_auth_file)
        self.cache_log = CacheDiagnostics(config)
        self.diagnostics = diagnostics(config, state)
        self.incidents = IncidentStore(config,self.diagnostics)
        self._local_auth = None
        self._rejected_auth = None
        def client(**kwargs):
            selected_transport = transport
            proxy = config.adapter.outbound_proxy
            if transport is None and proxy:
                selected_transport = SafeProxyTransport(proxy)
                proxy = None  # A client proxy mount would bypass our transport.
            return httpx.AsyncClient(trust_env=False, follow_redirects=False,
                transport=selected_transport, proxy=proxy, **kwargs)

        self.models = client(timeout=httpx.Timeout(
            connect=config.bridge.connect_seconds, read=config.bridge.idle_seconds, write=30, pool=5))
        self.control = client(timeout=10)

    async def probe(self):
        # Local initialization only. Never poll OpenAI or infer reachability here.
        self.diagnostics.observe_auth(self.config.adapter.existing_auth_file)
        try:
            signature = self._auth_signature()
            if signature != self._local_auth:
                self._local_auth = signature
                self._rejected_auth = None
                self.state.auth_state = "unknown"
            self.state.adapter_status = "running"
            self.state.adapter_ready = self.diagnostics.expired is not True and signature != self._rejected_auth
            if not self.state.adapter_ready:
                self.state.auth_state = "reauth_required"
            reason = "ok" if self.state.adapter_ready else "auth_unavailable"
        except (OSError, ValueError, TypeError):
            self.state.adapter_ready, self.state.adapter_status = False, "error"
            self.state.auth_state = "reauth_required"
            reason = "auth_unavailable"
        self.diagnostics.readiness(self.state.adapter_ready, reason)
        return self.state.adapter_ready

    def _auth_signature(self):
        access, account, _ = self.auth.read()
        return hashlib.sha256((access + "\0" + account).encode()).digest()

    async def catalog(self):
        async with asyncio.timeout(10), self.control.stream("GET", BASE + "/models?client_version=" + self.config.adapter.catalog_client_version, headers=self.auth.headers()) as r:
            if r.status_code != 200:
                return httpx.Response(r.status_code, headers=r.headers, json={"error": {"code": "upstream_error"}})
            raw = bytearray()
            async for chunk in r.aiter_bytes():
                if len(raw) + len(chunk) > 1024 * 1024:
                    raise ValueError("catalog_too_large")
                raw.extend(chunk)
        payload = json.loads(raw)
        models = payload.get("models")
        if not isinstance(models, list):
            raise ValueError("invalid_catalog")
        ids = [m["slug"] for m in models if isinstance(m, dict) and isinstance(m.get("slug"), str)
               and m.get("supported_in_api", True)]
        return httpx.Response(200, json={"object": "list", "data": [{"id": m} for m in ids]})

    async def open(self, method, path, body=None, request_headers=None):
        request_id = request_headers.get("x-relay-request-id") if request_headers else None
        if method == "GET" and path == "/v1/models":
            return await self.catalog()
        if method != "POST" or path != "/v1/responses":
            raise ValueError("adapter_route_denied")
        try:
            headers = self.auth.headers()
            signature = hashlib.sha256((headers["Authorization"][7:] + "\0" + headers["ChatGPT-Account-ID"]).encode()).digest()
        except (OSError, ValueError, TypeError):
            return httpx.Response(401, json={"error": {"code": "existing_login_unavailable"}})
        payload = json.loads(body)
        stream = payload.get("stream", False)
        # Codex backend transport is streaming, stateless Responses. Other fields
        # including tool-call IDs, outputs and encrypted reasoning remain intact.
        payload["stream"], payload["store"] = True, False
        payload.setdefault("instructions", "")
        if isinstance(payload.get("input"), str):
            payload["input"] = [{"role": "user", "content": [{"type": "input_text", "text": payload["input"]}]}]
        headers["Accept"] = "text/event-stream"
        headers.update(routing_headers(request_headers))
        started = time.monotonic()
        record_id = await self.cache_log.begin(payload, request_headers, headers.get("ChatGPT-Account-ID"), stream)
        incident=self.incidents.begin(body,request_id,record_id)
        def build_request():
            request=self.models.build_request("POST",BASE+"/responses",headers=headers,json=payload)
            if incident:incident.sent(request.content)
            return request
        try:
            r = await send_with_retry(self.models,
                build_request,
                attempts=self.config.adapter.safe_retry_attempts,
                budget=self.config.adapter.safe_retry_budget_seconds,
                diagnostics=self.diagnostics, fields={"request_id": request_id, "cache_record_id": record_id},
                on_response=incident.response if incident else None)
        except BaseException as exc:
            if incident:
                incident.failed(exc);await incident.finish()
            self.diagnostics.emit("inference_open_failed", request_id=request_id, cache_record_id=record_id,
                duration_ms=round((time.monotonic()-started)*1000), **exception_info(exc))
            with anyio.move_on_after(3, shield=True):
                await self.cache_log.update(record_id, ended_at=now(), transport_outcome="upstream_open_failed",
                    duration_ms=round((time.monotonic()-started)*1000))
            raise
        if r.status_code in (401, 403):
            self._local_auth = signature
            self._rejected_auth = signature
            self.state.auth_state = "reauth_required"
        elif r.status_code == 200:
            self._local_auth = signature
            self._rejected_auth = None
            self.state.auth_state = "ready"
        if r.status_code != 200:
            if incident:await incident.finish()
            self.diagnostics.emit("inference_http_error", request_id=request_id, cache_record_id=record_id,
                http_status=r.status_code, error_code=http_code(r.status_code),
                duration_ms=round((time.monotonic()-started)*1000))
        self.diagnostics.emit("inference_opened", request_id=request_id, cache_record_id=record_id,
            http_status=r.status_code, duration_ms=round((time.monotonic()-started)*1000))
        r.stream = DiagnosticStream(r.stream, self.cache_log, record_id, r.status_code, started, self.diagnostics, request_id)
        if r.status_code==200:
            r.stream=CompleteOutputStream(r.stream,self.config.bridge.max_body_bytes,self.cache_log,record_id,incident)
        with anyio.move_on_after(3, shield=True):
            await self.cache_log.update(record_id, http_status=r.status_code,
                response_turn_state_fp=self.cache_log.digest(routing_headers(r.headers, response=True).get("x-codex-turn-state")))
        if r.status_code == 200 and not r.headers.get("content-type"):
            # Observed backend omits this header while emitting Responses SSE.
            r.headers["content-type"] = "text/event-stream"
        if stream or r.status_code != 200:
            return r
        # Synthesize the nonstream response from the one unambiguous terminal
        # response object, not by concatenating text deltas or manufacturing usage.
        framer, terminal, size, output_items = SSEFramer(), None, 0, {}
        try:
            async for chunk in r.aiter_raw():
                size += len(chunk)
                if size > self.config.bridge.max_body_bytes:
                    raise ValueError("response_too_large")
                for frame in framer.feed(chunk):
                    data = "\n".join(line[5:].lstrip(" ") for line in frame.decode().splitlines() if line.startswith("data:"))
                    try:
                        obj = json.loads(data)
                    except ValueError:
                        continue
                    if isinstance(obj, dict) and obj.get("type") in ("response.completed", "response.failed", "response.incomplete"):
                        candidate = obj.get("response")
                        if terminal is not None and terminal != candidate:
                            raise ValueError("conflicting_terminal")
                        terminal = candidate
                    elif isinstance(obj, dict) and obj.get("type") == "response.output_item.done":
                        index, item = obj.get("output_index"), obj.get("item")
                        if type(index) is int and index >= 0 and isinstance(item, dict):
                            if index in output_items and output_items[index] != item:
                                raise ValueError("conflicting_output_item")
                            output_items[index] = item
                    elif isinstance(obj, dict) and obj.get("type") == "error":
                        raise ValueError("upstream_error")
            if framer.buffer or not isinstance(terminal, dict) or framer.conflict:
                raise ValueError("missing_terminal")
            if not terminal.get("output") and output_items:
                terminal = dict(terminal, output=[output_items[i] for i in sorted(output_items)])
            return httpx.Response(200, json=terminal, headers=routing_headers(r.headers, response=True))
        except Exception as exc:
            if incident:
                incident.failed(exc);await incident.finish()
            reason=str(exc) if isinstance(exc,ValueError) and str(exc) in {
                'response_too_large','conflicting_terminal','conflicting_output_item','upstream_error',
                'missing_terminal','conflicting_output_text','invalid_output_item','invalid_output_content',
                'missing_output_content_part','invalid_terminal','invalid_terminal_output','event_after_terminal'} else None
            self.diagnostics.emit('response_assembly_failed',request_id=request_id,cache_record_id=record_id,
                stage='nonstream_assembly',protocol_failure=reason,**exception_info(exc))
            await self.cache_log.update(record_id,response_assembly_error=reason or 'transport_error')
            raise
        finally:
            with anyio.move_on_after(4, shield=True):
                await r.aclose()

    async def close(self):
        await asyncio.gather(self.models.aclose(), self.control.aclose())
