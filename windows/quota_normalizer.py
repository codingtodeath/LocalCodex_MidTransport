import copy
import math
import re
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation

from .state import utcnow


def text(value, limit):
    # Opaque upstream identity/name strings are not telemetry fields.
    return value if isinstance(value, str) and re.fullmatch(r"[a-zA-Z0-9_.:-]{1," + str(limit) + r"}", value) else None


def unix_utc(value):
    if value is None:
        return None
    if isinstance(value, bool) or not isinstance(value, (float, int)) or not math.isfinite(value):
        raise ValueError("invalid_reset")
    try:
        return datetime.fromtimestamp(value, timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")
    except (ValueError, OverflowError, OSError):
        raise ValueError("invalid_reset") from None


def empty_quota(epoch, alias=None, identity="unverified", source="codex_app_server"):
    return dict(status="unavailable", source=source, captured_at=None, last_attempt_at=utcnow(),
                account_epoch=epoch, account_alias=alias, identity_check=identity, plan_type=None,
                windows=[], credits=None, error_code="not_yet_queried")


def failed_quota(previous, status, code):
    snapshot = copy.deepcopy(previous)
    snapshot.update(status=status, error_code=code, last_attempt_at=utcnow())
    return snapshot


def normalize(raw, previous):
    result = copy.deepcopy(previous)
    result["last_attempt_at"] = utcnow()
    if result["identity_check"] not in ("manual_confirmed", "automatic_match"):
        return failed_quota(result, "account_mismatch" if result["identity_check"] == "mismatch" else "unavailable", "identity_unverified")
    buckets = raw.get("rateLimitsByLimitId")
    if not isinstance(buckets, dict) or not buckets:
        legacy = raw.get("rateLimits")
        buckets = {legacy.get("limitId", "codex"): legacy} if isinstance(legacy, dict) else {}
    windows, credits, plan = [], None, None
    try:
        for index, (bucket_id, bucket) in enumerate(buckets.items()):
            if not isinstance(bucket, dict):
                raise ValueError("invalid_bucket")
            # Preserve recognized public bucket IDs; scrub arbitrary user strings.
            bucket_id = text(bucket_id, 96) or f"bucket_{index}"
            plan = text(bucket.get("planType"), 64) or plan
            for window_id, window in bucket.items():
                if not isinstance(window, dict) or "usedPercent" not in window:
                    continue
                used = window["usedPercent"]
                minutes = window.get("windowDurationMins")
                if isinstance(used, bool) or not isinstance(used, (int, float)) or not math.isfinite(used) or used < 0:
                    raise ValueError("invalid_percent")
                if minutes is not None and (type(minutes) is not int or minutes < 1):
                    raise ValueError("invalid_window")
                windows.append(dict(bucket_id=bucket_id, window_id=text(window_id, 96) or "unknown",
                                    display_name=None, used_percent=used, window_minutes=minutes,
                                    resets_at=unix_utc(window.get("resetsAt"))))
            c = bucket.get("credits")
            if isinstance(c, dict) and credits is None:
                balance = c.get("balance")
                if balance is not None:
                    try:
                        d = Decimal(str(balance))
                        if not d.is_finite():
                            raise InvalidOperation
                        balance = format(d, "f")
                        if len(balance) > 64:
                            raise InvalidOperation
                    except (InvalidOperation, ValueError):
                        raise ValueError("invalid_credits") from None
                credits = dict(available=c.get("hasCredits") if type(c.get("hasCredits")) is bool else None,
                               unlimited=c.get("unlimited") if type(c.get("unlimited")) is bool else None,
                               balance=balance, unit="credits")
        if len(windows) > 64:
            raise ValueError("too_many_windows")
    except (ValueError, TypeError):
        return failed_quota(previous, "unavailable", "quota_invalid")
    if not windows and credits is None:
        return failed_quota(previous, "unavailable", "quota_fields_missing")
    # Only this successful path moves captured_at; heartbeat/cache replay never does.
    result.update(status="ok", windows=windows, credits=credits, plan_type=plan,
                  captured_at=utcnow(), error_code=None)
    return result
