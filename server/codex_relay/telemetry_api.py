"""遥测接收：Windows -> Ubuntu。

见 01_DEVELOPMENT_SPEC.md 第 8、9 节与 contracts/README.md。
* 仅 `POST /internal/v1/telemetry`，仅接受 `tel_...`。
* body 上限 64 KiB；Schema `additionalProperties:false` 运行时生效。
* 被拒绝的 body 不写日志（不打印原始内容）。
* worker_id 必须与认证 Key 绑定；未知版本拒绝而不是猜字段语义。
* 心跳响应最多携带一个 refresh_quota 命令，形状由本项目定义。
"""

from __future__ import annotations

import json

from fastapi import APIRouter, Request, Response
from fastapi.responses import JSONResponse
from pydantic import ValidationError
from jsonschema.exceptions import ValidationError as SchemaError

from . import auth as auth_mod
from . import commands as cmd
from . import quota_store, worker_state
from .db import utc_now
from .models import Telemetry, TelemetryReply
from .services import Services
from . import contracts
from .api_proxy import read_bounded_body

router = APIRouter()


def _bearer(request: Request) -> str | None:
    header = request.headers.get("authorization") or ""
    if not header.lower().startswith("bearer "):
        return None
    token = header[7:].strip()
    return token or None


def _error(status_code: int, code: str, message: str) -> JSONResponse:
    return JSONResponse(
        status_code=status_code,
        content={"error": {"message": message, "type": "invalid_request_error", "code": code}},
    )


@router.post("/internal/v1/telemetry")
async def ingest_telemetry(request: Request) -> Response:
    services: Services = request.app.state.services
    config = services.config
    db = services.db

    # --- 角色鉴权：只接受 tel_，且必须是 telemetry 角色 -----------------
    token = _bearer(request)
    if token is None:
        return _error(401, "invalid_api_key", "Missing telemetry credential.")
    worker_from_key = auth_mod.verify_worker_credential(db, auth_mod.ROLE_TELEMETRY, token, services.pepper)
    if worker_from_key is None:
        # 不区分“不存在”和“已撤销”，不回显 Key。
        return _error(401, "invalid_api_key", "Invalid telemetry credential.")

    # --- 大小上限（解析前） ------------------------------------------
    limit = config.max_telemetry_bytes
    raw = await read_bounded_body(request, limit)

    # --- JSON + Schema（additionalProperties:false 运行时生效） -------
    try:
        payload = contracts.load_json(raw)
    except (ValueError, UnicodeError, RecursionError):
        return _error(400, "invalid_request", "Telemetry body is not valid JSON.")

    try:
        contracts.validate("telemetry", payload)
        telemetry = Telemetry.model_validate(payload)
    except (ValidationError, SchemaError, RecursionError):
        # Unknown field names are user-controlled and may themselves be secrets.
        from .db import audit

        audit(
            db,
            "telemetry",
            "rejected",
            actor=worker_from_key,
            detail={"category": "schema_validation_failed"},
        )
        return _error(400, "invalid_telemetry", "Telemetry payload failed validation.")

    if telemetry.worker_id != worker_from_key:
        from .db import audit

        audit(
            db,
            "telemetry",
            "worker_mismatch",
            actor=worker_from_key,
            detail={"category": "credential_worker_mismatch"},
        )
        return _error(403, "forbidden_role", "Credential is not bound to this worker.")

    if config.is_production and telemetry.quota is not None and telemetry.quota.source == "mock":
        return _error(400, "mock_not_allowed", "Synthetic telemetry is disabled in production.")

    worker_state.ensure_worker(db, telemetry.worker_id)

    # --- 心跳：seq 去重 + 旧 instance 隔离 --------------------------
    outcome = worker_state.record_heartbeat(
        db,
        worker_id=telemetry.worker_id,
        instance_id=telemetry.instance_id,
        seq=telemetry.seq,
        sent_at=telemetry.sent_at,
        capabilities=telemetry.capabilities.model_dump(),
        local=telemetry.local.model_dump(),
    )

    # --- 额度：只在新包上处理，重复包不覆盖状态 ----------------------
    if outcome.accepted:
        for result in telemetry.command_results:
            cmd.record_command_result(db, telemetry.worker_id, result.id, result.status, result.error_code)
        if telemetry.quota is not None:
            result = quota_store.record_quota(db, telemetry.worker_id, telemetry.quota)
            if result.startswith("failure:"):
                from .db import audit

                audit(
                    db,
                    "quota",
                    "query_failed",
                    actor=telemetry.worker_id,
                    detail={"category": result.split(":", 1)[1]},
                )
        if telemetry.account_usage is not None:
            quota_store.record_account_usage(db, telemetry.worker_id, telemetry.account_usage)

    # --- 响应：最多一个命令 -----------------------------------------
    snapshot = worker_state.evaluate(db, telemetry.worker_id, config)
    command = cmd.take_pending_command(db, telemetry.worker_id) if outcome.accepted else None

    reply = TelemetryReply(
        ack_seq=outcome.ack_seq,
        server_time=utc_now(),
        commands=[],
    )
    body = reply.model_dump()
    if command is not None:
        body["commands"] = [command]

    response = JSONResponse(status_code=200, content=body)
    if outcome.reason:
        response.headers["X-Relay-Heartbeat"] = outcome.reason[:120]
    response.headers["X-Relay-Worker-State"] = snapshot.state
    return response
