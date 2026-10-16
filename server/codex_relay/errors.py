"""对外错误语义。

见 01_DEVELOPMENT_SPEC.md 6.3。对外错误正文使用与 OpenAI 兼容的形状：

    {"error": {"message": ..., "type": ..., "code": ...}}

约束：
* 不得把 OAuth token、账号邮箱、完整上游错误页或配置路径放进 message。
* 一旦 SSE 200 已发出，就不能再改 HTTP 状态码；流中断只记录 interrupted。
"""

from __future__ import annotations

from typing import Any


class RelayError(Exception):
    """带对外状态码与稳定 error.code 的应用异常。"""

    def __init__(
        self,
        status_code: int,
        code: str,
        message: str,
        *,
        error_type: str = "server_error",
        retry_after: int | None = None,
        headers: dict[str, str] | None = None,
    ) -> None:
        super().__init__(message)
        self.status_code = status_code
        self.code = code
        self.message = message
        self.error_type = error_type
        self.retry_after = retry_after
        self.headers = headers or {}

    def body(self) -> dict[str, Any]:
        return {
            "error": {
                "message": self.message,
                "type": self.error_type,
                "code": self.code,
            }
        }

    def response_headers(self) -> dict[str, str]:
        headers = dict(self.headers)
        if self.retry_after is not None:
            headers["Retry-After"] = str(self.retry_after)
        return headers


# --- 客户端鉴权 ---------------------------------------------------------


def invalid_api_key() -> RelayError:
    return RelayError(
        401,
        "invalid_api_key",
        "Invalid relay gateway key.",
        error_type="invalid_request_error",
        headers={"WWW-Authenticate": "Bearer"},
    )


def model_not_allowed(model: str | None) -> RelayError:
    # 不回显完整模型名以外的信息，也不提示“哪个模型才允许”。
    return RelayError(
        403,
        "model_not_allowed",
        "Requested model is not in the relay allowlist.",
        error_type="invalid_request_error",
    )


def gateway_concurrency_limit(retry_after: int = 5) -> RelayError:
    return RelayError(
        429,
        "gateway_concurrency_limit",
        "Relay local concurrency limit reached.",
        error_type="rate_limit_error",
        retry_after=retry_after,
    )


def worker_unavailable() -> RelayError:
    return RelayError(
        503,
        "worker_unavailable",
        "Windows worker is unavailable.",
    )


def adapter_unavailable() -> RelayError:
    return RelayError(
        503,
        "adapter_unavailable",
        "Windows local adapter is not ready.",
    )


def upstream_reauth_required() -> RelayError:
    return RelayError(
        503,
        "upstream_reauth_required",
        "The upstream subscription session requires re-authentication on Windows.",
    )


def upstream_timeout() -> RelayError:
    return RelayError(504, "upstream_timeout", "Upstream worker timed out.")


def upstream_protocol_error() -> RelayError:
    return RelayError(
        502, "upstream_protocol_error", "Upstream returned an unexpected response."
    )


def metering_unavailable() -> RelayError:
    """数据库写入失败时使用。见 01_DEVELOPMENT_SPEC.md 第 10 节。"""
    return RelayError(
        503,
        "metering_unavailable",
        "Relay metering storage is unavailable; refusing to serve unmetered traffic.",
    )


def payload_too_large(limit: int) -> RelayError:
    return RelayError(
        413,
        "payload_too_large",
        f"Request body exceeds the {limit} byte relay limit.",
        error_type="invalid_request_error",
    )


def invalid_request(message: str = "Invalid request body.") -> RelayError:
    return RelayError(400, "invalid_request", message, error_type="invalid_request_error")


def unsupported_media_type() -> RelayError:
    return RelayError(
        415,
        "unsupported_media_type",
        "Compressed or unsupported request encodings are not accepted.",
        error_type="invalid_request_error",
    )


def not_authenticated() -> RelayError:
    return RelayError(
        401,
        "not_authenticated",
        "Authentication required.",
        error_type="invalid_request_error",
    )


def csrf_failed() -> RelayError:
    return RelayError(
        403,
        "csrf_failed",
        "CSRF validation failed.",
        error_type="invalid_request_error",
    )


def forbidden_role() -> RelayError:
    return RelayError(
        403,
        "forbidden_role",
        "This credential is not valid for this endpoint.",
        error_type="invalid_request_error",
    )
