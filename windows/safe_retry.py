"""Retry only unsubmitted requests and explicit temporary rejections.

One owner (the Windows adapter), one deadline, and no replay after acceptance.
The request body and credentials never enter diagnostics.
"""
import asyncio
from asyncio import sleep
import json
import math
import random
import ssl
import time
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime

import httpx
import anyio

from .runtime_diagnostics import exception_info

ERROR_BODY_LIMIT = 65536


def retry_after(value):
    if not value or len(value) > 128:
        return None
    try:
        if value.strip().isdigit():
            seconds = float(value.strip())
        else:
            date = parsedate_to_datetime(value)
            if date.tzinfo is None:
                date = date.replace(tzinfo=timezone.utc)
            seconds = max(0., (date - datetime.now(timezone.utc)).total_seconds())
        return seconds if math.isfinite(seconds) else None
    except (ValueError, TypeError, OverflowError):
        return None


def error_category(response):
    # The status alone does not prove an inference was rejected before execution.
    if not response.extensions.get("relay_error_body_complete", True):
        return None
    try:
        obj = response.json()
        if not isinstance(obj, dict) or any(k in obj for k in ("usage", "response", "output")):
            return None
        error = obj.get("error")
        if not isinstance(error, dict):
            return None
        code, kind = error.get("code"), error.get("type")
        if not isinstance(code, str) or (kind is not None and not isinstance(kind, str)):
            return None
        if code in {"insufficient_quota", "quota_exceeded", "billing_hard_limit_reached",
                    "subscription_sharing_usage_limit_exceeded", "subscription_sharing_usage_unavailable"}:
            return "quota_exhausted"
        if response.status_code == 429 and kind == "rate_limit_error" and code in {"rate_limit_exceeded", "slow_down"}:
            return "temporary_rate_limit"
        if response.status_code == 503 and kind in {"server_error", "service_unavailable_error", "rate_limit_error"}:
            if code == "server_is_overloaded":
                return "temporary_overload"
            if code == "slow_down":
                return "temporary_rate_limit"
    except (ValueError, UnicodeError):
        pass
    return None


def connection_retryable(exc, request_sent):
    if request_sent or not isinstance(exc, (httpx.ConnectError, httpx.ConnectTimeout)):
        return False
    current = exc
    for _ in range(8):
        if isinstance(current, ssl.SSLCertVerificationError) or (
                isinstance(current, ssl.SSLError) and getattr(current, "reason", None) == "CERTIFICATE_VERIFY_FAILED"):
            return False
        current = getattr(current, "__cause__", None) or getattr(current, "__context__", None)
        if current is None:
            break
    return True


async def buffered_error(response):
    data, complete = bytearray(), True
    try:
        async for chunk in response.aiter_bytes():
            remaining = ERROR_BODY_LIMIT - len(data)
            data.extend(chunk[:remaining])
            if len(chunk) > remaining:
                complete = False
                break
    finally:
        with anyio.move_on_after(4, shield=True):
            await response.aclose()
    headers = {k: v for k, v in response.headers.items() if k not in {"content-length", "content-encoding"}}
    return httpx.Response(response.status_code, content=bytes(data), headers=headers,
                          request=response.request, extensions={"relay_error_body_complete": complete})


async def send_with_retry(client, build_request, *, attempts, budget, diagnostics, fields, on_response=None):
    deadline = time.monotonic() + budget
    last_error = None
    last_response = None
    for number in range(1, attempts + 1):
        sent = False

        async def trace(event, info):
            nonlocal sent
            if event.endswith(("send_request_headers.started", "send_request_body.started")):
                # HTTP proxy CONNECT sets up the tunnel; it cannot submit inference.
                traced_request = info.get('request')
                if getattr(traced_request, 'method', None) not in (b'CONNECT', 'CONNECT'):
                    sent = True

        request = build_request()
        request.extensions["trace"] = trace
        facts = dict(fields, attempt_no=number, max_attempts=attempts)
        diagnostics.emit("inference_attempt_started", **facts)
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            if last_response is not None:
                return last_response
            raise last_error or httpx.ConnectTimeout("upstream_retry_deadline", request=request)
        response = None
        try:
            async with asyncio.timeout(remaining):
                response = await client.send(request, stream=True)
                if on_response is not None:await on_response(response)
                # Acceptance ends retry eligibility, even if no SSE bytes arrived.
                if response.status_code == 200:
                    return response
                response = await buffered_error(response)
        except BaseException as exc:
            if response is not None:
                with anyio.move_on_after(4, shield=True):
                    await response.aclose()
            if isinstance(exc, TimeoutError):
                exc = httpx.ReadTimeout("upstream_open_deadline", request=request)
            allowed = connection_retryable(exc, sent)
            diagnostics.emit("inference_attempt_failed", **facts, request_sent=sent,
                stage="awaiting_response" if sent else "before_send", retryable=allowed, **exception_info(exc))
            if not allowed or number == attempts:
                raise exc
            last_error = exc
            last_response = None
            category = None
        else:
            category = error_category(response)
            last_response = response
            response.extensions["relay_error_category"] = category
            allowed = category in {"temporary_rate_limit", "temporary_overload"}
            diagnostics.emit("inference_attempt_failed", **facts, request_sent=True,
                stage="received_error", retryable=allowed, http_status=response.status_code,
                error_code="rate_limited" if category == "temporary_rate_limit" else "upstream_server_error" if category == "temporary_overload" else "upstream_quota_exhausted" if category == "quota_exhausted" else "http_error")
            if not allowed or number == attempts:
                return response
        hint = retry_after(response.headers.get("retry-after")) if response is not None else None
        delay = max(2 ** (number - 1), hint or 0) + random.uniform(0, .5)
        if delay >= deadline - time.monotonic():
            diagnostics.emit("inference_retry_exhausted", **facts, error_code="retry_budget_exhausted")
            if response is not None:
                return response
            raise last_error
        diagnostics.emit("inference_retry_scheduled", **facts, retry_wait_ms=round(delay * 1000),
                         retry_budget_ms=round(max(0, deadline - time.monotonic()) * 1000),
                         error_code="rate_limited" if category == "temporary_rate_limit" else "upstream_server_error" if category else exception_info(last_error)["error_code"])
        await sleep(delay)
    raise RuntimeError("unreachable_retry_state")
