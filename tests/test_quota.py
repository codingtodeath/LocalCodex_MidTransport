import json
import time
from pathlib import Path
import pytest

from windows.contracts import validate
from windows.identity import Identity
from windows.quota_normalizer import normalize, empty_quota, failed_quota
from windows.quota_service import QuotaService


def snapshot():
    return empty_quota("a2a973fe-0c86-4f4d-87c4-78d5f031dbae", "测试", "manual_confirmed")


def test_buckets_unknown_windows_missing_fields_no_duplicate():
    raw = {"rateLimits": {"primary": {"usedPercent": 99}}, "rateLimitsByLimitId": {
        "codex": {"primary": {"usedPercent": 25, "windowDurationMins": 300, "resetsAt": 1780000000},
                  "secondary": {"usedPercent": 110}, "future": {"usedPercent": 5}},
        "extra": {"primary": {"usedPercent": 2}}}}
    q = normalize(raw, snapshot())
    assert q["status"] == "ok" and len(q["windows"]) == 4
    assert q["windows"][0]["used_percent"] == 25
    assert q["windows"][1]["used_percent"] == 110
    assert q["windows"][1]["window_minutes"] is None
    assert q["windows"][0]["resets_at"].endswith("Z")
    assert q["credits"] is None


@pytest.mark.parametrize("value", [-1, float("nan"), float("inf"), True, "25"])
def test_invalid_usage_preserves_old_snapshot(value):
    old = snapshot()
    old["captured_at"] = "2020-01-01T00:00:00Z"
    q = normalize({"rateLimits": {"primary": {"usedPercent": value}}}, old)
    assert q["status"] == "unavailable" and q["captured_at"] == old["captured_at"]


def test_identity_required_and_no_account_details():
    q = snapshot()
    q["identity_check"] = "unverified"
    raw = {"email": "canary@example.com", "access_token": "CANARY", "rateLimits": {"primary": {"usedPercent": 25}}}
    result = normalize(raw, q)
    assert result["status"] != "ok" and result["windows"] == []
    assert "CANARY" not in json.dumps(result) and "@" not in json.dumps(result)


def test_credits_decimal_and_null_fields():
    q = normalize({"rateLimits": {"credits": {"balance": "0.1200", "hasCredits": True}}}, snapshot())
    assert q["credits"] == {"available": True, "unlimited": None, "balance": "0.1200", "unit": "credits"}


def test_manual_confirmation_revoked_on_either_file_change(config):
    config.quota.codex_home.joinpath("auth.json").write_text("synthetic_quota_login")
    config.adapter.config_file.parent.joinpath("proxy-auth/codex.json").write_text("synthetic_proxy_login")
    identity = Identity(config)
    identity.confirm()
    epoch = identity.epoch
    assert Identity(config).check == "manual_confirmed"
    config.adapter.config_file.parent.joinpath("proxy-auth/codex.json").write_text("new_synthetic_login")
    identity.reload()
    assert identity.check == "unverified" and identity.epoch != epoch


async def test_quota_min_interval_and_independent_proxy(config, state):
    service = QuotaService(config, state)
    state.auth_state = "ready"
    assert await service.refresh()
    captured = state.quota["captured_at"]
    assert not await service.refresh()
    assert state.quota["captured_at"] == captured and state.auth_state == "ready"
    await service.close()
