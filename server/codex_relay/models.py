"""协议对象模型（白名单严格校验）。

契约要求 `additionalProperties: false` 必须在运行时生效，不能只存在文档里。
这里用 Pydantic 的 `extra="forbid"` 实现；同时按 05 号文档与 contracts/README
的要求补充 Schema 之外的业务校验。

拒绝多余字段时，响应里不包含被拒绝的原始 body。
"""

from __future__ import annotations

import re
from datetime import UTC, datetime
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

WORKER_ID_RE = re.compile(r"^[a-zA-Z0-9_-]{1,48}$")
ERROR_CODE_RE = re.compile(r"^[a-z0-9_]+$")
BALANCE_RE = re.compile(r"^-?[0-9]+(?:\.[0-9]+)?$")


def _norm_dt(value: str | None) -> str | None:
    """把任意合法 ISO-8601 归一化为 UTC Z 形式。"""

    if value is None:
        return None
    text = value.strip()
    if text.endswith("Z"):
        text = text[:-1] + "+00:00"
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError as exc:
        raise ValueError("不是合法的 ISO-8601 时间") from exc
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=UTC)
    return parsed.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%S.%f")[:-3] + "Z"


class _Strict(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)


# --- 遥测：Windows -> Ubuntu -------------------------------------------


class Capabilities(_Strict):
    responses_http: bool
    responses_nonstream: bool
    quota_read: bool
    account_usage_read: bool


class LocalState(_Strict):
    bridge_ready: bool
    adapter_status: Literal["running", "stopped", "error"]
    adapter_ready: bool
    auth_state: Literal["ready", "reauth_required", "unknown"]
    active_requests: int = Field(ge=0, le=100)
    last_inference_success_at: str | None = None

    @field_validator("last_inference_success_at")
    @classmethod
    def _dt(cls, v: str | None) -> str | None:
        return _norm_dt(v)


class QuotaWindow(_Strict):
    bucket_id: str = Field(min_length=1, max_length=96)
    window_id: str = Field(min_length=1, max_length=96)
    display_name: str | None = Field(default=None, max_length=96)
    used_percent: float = Field(ge=0)
    window_minutes: int | None = Field(default=None, ge=1)
    resets_at: str | None = None

    @field_validator("resets_at")
    @classmethod
    def _dt(cls, v: str | None) -> str | None:
        return _norm_dt(v)


class Credits(_Strict):
    available: bool | None
    unlimited: bool | None
    balance: str | None = Field(default=None, max_length=64)
    unit: Literal["credits"] | None

    @field_validator("balance")
    @classmethod
    def _bal(cls, v: str | None) -> str | None:
        if v is not None and not BALANCE_RE.match(v):
            raise ValueError("credits.balance 必须是十进制字符串")
        return v


class Quota(_Strict):
    status: Literal["ok", "unsupported", "reauth_required", "unavailable", "account_mismatch"]
    source: Literal["codex_app_server", "none", "mock"]
    captured_at: str | None
    last_attempt_at: str
    account_epoch: str | None
    account_alias: str | None = Field(default=None, max_length=48)
    identity_check: Literal["automatic_match", "manual_confirmed", "unverified", "mismatch"]
    plan_type: str | None = Field(default=None, max_length=64)
    windows: list[QuotaWindow] = Field(default_factory=list, max_length=64)
    credits: Credits | None = None
    error_code: str | None = Field(default=None, max_length=64)

    @field_validator("captured_at", "last_attempt_at")
    @classmethod
    def _dt(cls, v: str | None) -> str | None:
        return _norm_dt(v)

    @field_validator("account_epoch")
    @classmethod
    def _epoch(cls, v: str | None) -> str | None:
        if v is None:
            return None
        return _norm_uuid(v)

    @field_validator("error_code")
    @classmethod
    def _code(cls, v: str | None) -> str | None:
        if v is not None and not ERROR_CODE_RE.match(v):
            raise ValueError("error_code 必须匹配 ^[a-z0-9_]+$")
        return v

    @field_validator("windows")
    @classmethod
    def _unique_windows(cls, v: list[QuotaWindow]) -> list[QuotaWindow]:
        seen: set[tuple[str, str]] = set()
        for w in v:
            key = (w.bucket_id, w.window_id)
            if key in seen:
                raise ValueError(f"同一 quota 内 bucket_id+window_id 重复：{key}")
            seen.add(key)
        return v

    @model_validator(mode="after")
    def _ok_requires_evidence(self) -> Quota:
        # contracts/README：status=ok 需有可用窗口或 credits。
        if self.status == "ok":
            if not self.windows and self.credits is None:
                raise ValueError("status=ok 必须提供 windows 或 credits")
            if self.captured_at is None:
                raise ValueError("status=ok 必须提供 captured_at")
            if self.account_epoch is None:
                raise ValueError("status=ok 必须提供 account_epoch")
            # identity_check 允许为 unverified/mismatch：Schema 只要求
            # ok 时有 captured_at 与 account_epoch。身份未确认的额度仍会
            # 被保存，但网站必须显式标注“仅供参考”，不能当作已确认账号额度。
        return self


class AccountUsageSummary(_Strict):
    lifetime_tokens: int | None = Field(default=None, ge=0)
    peak_daily_tokens: int | None = Field(default=None, ge=0)
    longest_running_turn_seconds: int | None = Field(default=None, ge=0)
    current_streak_days: int | None = Field(default=None, ge=0)
    longest_streak_days: int | None = Field(default=None, ge=0)


class DailyBucket(_Strict):
    start_date: str
    tokens: int = Field(ge=0)


class AccountUsage(_Strict):
    captured_at: str
    scope: Literal["upstream_account_summary"]
    summary: AccountUsageSummary
    daily_buckets: list[DailyBucket] | None = Field(default=None, max_length=90)

    @field_validator("captured_at")
    @classmethod
    def _dt(cls, v: str) -> str:
        return _norm_dt(v) or v


class CommandResult(_Strict):
    id: str
    status: Literal["accepted", "done", "failed", "expired"]
    error_code: str | None = Field(default=None, max_length=64)

    @field_validator("id")
    @classmethod
    def _uuid(cls, v: str) -> str:
        return _norm_uuid(v)

    @field_validator("error_code")
    @classmethod
    def _code(cls, v: str | None) -> str | None:
        if v is not None and not ERROR_CODE_RE.match(v):
            raise ValueError("error_code 必须匹配 ^[a-z0-9_]+$")
        return v


class Telemetry(_Strict):
    protocol_version: Literal["1.0"]
    worker_id: str
    instance_id: str
    seq: int = Field(ge=1)
    sent_at: str
    capabilities: Capabilities
    local: LocalState
    quota: Quota | None = None
    account_usage: AccountUsage | None = None
    command_results: list[CommandResult] = Field(default_factory=list, max_length=16)

    @field_validator("worker_id")
    @classmethod
    def _worker(cls, v: str) -> str:
        if not WORKER_ID_RE.match(v):
            raise ValueError("worker_id 必须是 1-48 位 [a-zA-Z0-9_-]")
        return v

    @field_validator("instance_id")
    @classmethod
    def _instance(cls, v: str) -> str:
        return _norm_uuid(v)

    @field_validator("sent_at")
    @classmethod
    def _dt(cls, v: str) -> str:
        result = _norm_dt(v)
        assert result is not None
        return result


# --- 健康：Bridge -> Ubuntu（经隧道读取） ------------------------------


class Health(_Strict):
    protocol_version: Literal["1.0"]
    worker_id: str
    instance_id: str
    observed_at: str
    local: LocalState

    @field_validator("worker_id")
    @classmethod
    def _worker(cls, v: str) -> str:
        if not WORKER_ID_RE.match(v):
            raise ValueError("worker_id 必须是 1-48 位 [a-zA-Z0-9_-]")
        return v

    @field_validator("instance_id")
    @classmethod
    def _instance(cls, v: str) -> str:
        return _norm_uuid(v)

    @field_validator("observed_at")
    @classmethod
    def _dt(cls, v: str) -> str:
        result = _norm_dt(v)
        assert result is not None
        return result


# --- 心跳响应：Ubuntu -> Windows ---------------------------------------


class CommandEnvelope(BaseModel):
    model_config = ConfigDict(extra="forbid")
    id: str
    type: Literal["refresh_quota"]
    expires_at: str


class TelemetryReply(BaseModel):
    model_config = ConfigDict(extra="forbid")
    protocol_version: Literal["1.0"] = "1.0"
    ack_seq: int = Field(ge=1)
    server_time: str
    commands: list[CommandEnvelope] = Field(default_factory=list, max_length=1)


# --- usage 归一化 -------------------------------------------------------


class NormalizedUsage(BaseModel):
    """从 Responses JSON / 终结事件中提取的规范化用量。

    空值用 None；零只表示明确报告的零。
    """

    input_tokens: int | None = None
    cached_input_tokens: int | None = None
    cache_write_tokens: int | None = None
    output_tokens: int | None = None
    reasoning_tokens: int | None = None
    total_tokens: int | None = None
    status: Literal["reported", "partial", "missing", "invalid"] = "missing"
    problem: str | None = None

    def as_columns(self) -> dict[str, Any]:
        return {
            "input_tokens": self.input_tokens,
            "cached_input_tokens": self.cached_input_tokens,
            "cache_write_tokens": self.cache_write_tokens,
            "output_tokens": self.output_tokens,
            "reasoning_tokens": self.reasoning_tokens,
            "total_tokens": self.total_tokens,
            "usage_status": self.status,
        }


def _norm_uuid(value: str) -> str:
    import uuid

    try:
        return str(uuid.UUID(str(value)))
    except (ValueError, AttributeError, TypeError) as exc:
        raise ValueError("必须是合法 UUID") from exc


def extract_usage(payload: dict[str, Any] | None) -> NormalizedUsage:
    """把上游 usage 对象映射为规范化字段。

    映射见 01_DEVELOPMENT_SPEC.md 第 10 节：
      input_tokens -> input_tokens
      input_tokens_details.cached_tokens -> cached_input_tokens
      input_tokens_details.cache_write_tokens -> cache_write_tokens
      output_tokens_details.reasoning_tokens -> reasoning_tokens
    缓存是输入的子集；reasoning 是输出明细，不额外累加。

    不按文本长度猜测 token；缺字段保留 None，不自动补零。
    """

    if not isinstance(payload, dict) or not payload:
        return NormalizedUsage(status="missing", problem="no_usage_object")

    def _int(value: Any) -> int | None:
        if value is None:
            return None
        if isinstance(value, bool):
            raise ValueError("token 字段不能是布尔值")
        if isinstance(value, int):
            return value
        if isinstance(value, float) and value.is_integer():
            return int(value)
        raise ValueError("token 字段必须是整数")

    try:
        input_tokens = _int(payload.get("input_tokens"))
        output_tokens = _int(payload.get("output_tokens"))
        total_tokens = _int(payload.get("total_tokens"))
        details_in = payload.get("input_tokens_details")
        details_out = payload.get("output_tokens_details")
        cached = None
        cache_write = None
        if isinstance(details_in, dict):
            cached = _int(details_in.get("cached_tokens"))
            cache_write = _int(details_in.get("cache_write_tokens"))
        reasoning = None
        if isinstance(details_out, dict):
            reasoning = _int(details_out.get("reasoning_tokens"))
    except ValueError as exc:
        return NormalizedUsage(status="invalid", problem=str(exc))

    usage = NormalizedUsage(
        input_tokens=input_tokens,
        cached_input_tokens=cached,
        cache_write_tokens=cache_write,
        output_tokens=output_tokens,
        reasoning_tokens=reasoning,
        total_tokens=total_tokens,
    )

    # 负值一律 invalid（原始证据不悄悄改写，但也不产生看似正常的价格）。
    for name in (
        "input_tokens",
        "cached_input_tokens",
        "cache_write_tokens",
        "output_tokens",
        "reasoning_tokens",
        "total_tokens",
    ):
        value = getattr(usage, name)
        if value is not None and value < 0:
            usage.status = "invalid"
            usage.problem = f"negative_{name}"
            return usage

    # 必须至少有输入或输出才有计量意义。
    if usage.input_tokens is None and usage.output_tokens is None:
        usage.status = "missing"
        usage.problem = "no_token_fields"
        return usage

    # 缓存是输入的子集：0 <= H <= I。
    if usage.cached_input_tokens is not None:
        if usage.input_tokens is None:
            usage.status = "invalid"
            usage.problem = "cached_without_input"
            return usage
        if usage.cached_input_tokens > usage.input_tokens:
            usage.status = "invalid"
            usage.problem = "cached_input_exceeds_input"
            return usage

    # total 与 input/output 的一致性：仅在有全部三项时判断。
    if (
        usage.total_tokens is not None
        and usage.input_tokens is not None
        and usage.output_tokens is not None
        and usage.total_tokens < usage.input_tokens + usage.output_tokens
    ):
        usage.status = "invalid"
        usage.problem = "total_less_than_parts"
        return usage

    # 三个必要项齐全才算 reported；否则 partial。
    if usage.input_tokens is not None and usage.output_tokens is not None:
        usage.status = "reported"
    else:
        usage.status = "partial"
        usage.problem = "incomplete_token_fields"
    return usage
