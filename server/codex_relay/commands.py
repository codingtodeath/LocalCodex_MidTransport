"""短期命令：仅 `refresh_quota`。

见 01_DEVELOPMENT_SPEC.md 第 9 节。约束：
* 命令类型白名单只有 `refresh_quota`；不存在执行 shell、读文件、返回 token、
  任意 URL 请求或更换上游等命令。
* TTL 默认 60 秒；过期为 expired。
* 全局最短刷新间隔 30 秒；网页连续点击只创建一个命令。
* 同一命令重复下发是幂等的；重复结果不重复计数。
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

from .db import Database, iso, parse_iso, utc_now, audit

COMMAND_REFRESH_QUOTA = "refresh_quota"
ALLOWED_COMMANDS = (COMMAND_REFRESH_QUOTA,)


@dataclass
class CommandDecision:
    created: bool
    command_id: str | None
    status: str
    reason: str | None = None
    expires_at: str | None = None


def expire_stale_commands(db: Database, now: datetime | None = None) -> int:
    """把已过期但仍 pending/delivered 的命令标记为 expired。"""

    now = now or datetime.now(UTC)
    with db.write() as conn:
        cur = conn.execute(
            "UPDATE commands SET status = 'expired', updated_at = ? "
            "WHERE status IN ('pending','delivered') AND expires_at <= ?",
            (iso(now), iso(now)),
        )
        return cur.rowcount or 0


def last_refresh_command_at(db: Database, worker_id: str) -> datetime | None:
    row = db.query_one(
        "SELECT created_at FROM commands WHERE worker_id = ? AND command_type = ? "
        "ORDER BY created_at DESC LIMIT 1",
        (worker_id, COMMAND_REFRESH_QUOTA),
    )
    if row is None:
        return None
    return parse_iso(row["created_at"])


def create_refresh_command(
    db: Database,
    worker_id: str,
    config: Any,
    *,
    worker_servable: bool,
    now: datetime | None = None,
    actor: str = "admin",
) -> CommandDecision:
    """创建 refresh_quota 命令，带 TTL 与最短间隔去抖。"""

    now = now or datetime.now(UTC)
    expire_stale_commands(db, now)

    if not worker_servable:
        # Windows 离线时页面显示不可刷新，不暗示立即获得实时数据。
        return CommandDecision(False, None, "worker_offline", "worker_offline")

    # 已有未执行的命令：直接复用，避免堆积。
    pending = db.query_one(
        "SELECT command_id, expires_at FROM commands WHERE worker_id = ? AND command_type = ? "
        "AND status = 'pending' AND expires_at > ? ORDER BY created_at DESC LIMIT 1",
        (worker_id, COMMAND_REFRESH_QUOTA, iso(now)),
    )
    if pending is not None:
        return CommandDecision(
            False, pending["command_id"], "already_pending", "command_already_pending", pending["expires_at"]
        )

    last = last_refresh_command_at(db, worker_id)
    if last is not None:
        elapsed = (now - last).total_seconds()
        if elapsed < config.command_min_refresh_interval_seconds:
            return CommandDecision(
                False,
                None,
                "debounced",
                f"min_interval_{config.command_min_refresh_interval_seconds}s_not_elapsed",
            )

    command_id = str(uuid.uuid4())
    expires_at = iso(now + timedelta(seconds=config.command_ttl_seconds))
    with db.write() as conn:
        conn.execute(
            "INSERT INTO commands (command_id, worker_id, command_type, created_at, expires_at, "
            "delivered_at, status, updated_at) VALUES (?,?,?,?,?,NULL,'pending',?)",
            (command_id, worker_id, COMMAND_REFRESH_QUOTA, iso(now), expires_at, iso(now)),
        )
    audit(db, "command", "created", actor=actor, detail={"worker_id": worker_id, "type": COMMAND_REFRESH_QUOTA})
    return CommandDecision(True, command_id, "created", None, expires_at)


def take_pending_command(db: Database, worker_id: str, now: datetime | None = None) -> dict[str, Any] | None:
    """取出一个待下发命令并标记为已投递。心跳响应最多携带一个命令。"""

    now = now or datetime.now(UTC)
    expire_stale_commands(db, now)
    with db.write() as conn:
        row = conn.execute(
            "SELECT command_id, command_type, expires_at FROM commands "
            "WHERE worker_id = ? AND status = 'pending' AND expires_at > ? "
            "ORDER BY created_at ASC LIMIT 1",
            (worker_id, iso(now)),
        ).fetchone()
        if row is None:
            return None
        conn.execute(
            "UPDATE commands SET status = 'delivered', delivered_at = ?, updated_at = ? "
            "WHERE command_id = ?",
            (iso(now), iso(now), row["command_id"]),
        )
    return {
        "id": row["command_id"],
        "type": row["command_type"],
        "expires_at": row["expires_at"],
    }


def record_command_result(
    db: Database,
    worker_id: str,
    command_id: str,
    status: str,
    error_code: str | None = None,
) -> bool:
    """记录命令执行结果。未知命令、属于其他 worker 的命令一律忽略。"""

    if status not in ("accepted", "done", "failed", "expired"):
        return False
    now = utc_now()
    with db.write() as conn:
        cur = conn.execute(
            "UPDATE commands SET status = ?, error_code = ?, updated_at = ? "
            "WHERE command_id = ? AND worker_id = ? AND status IN ('delivered', 'accepted') "
            "AND status != ?",
            (status, error_code, now, command_id, worker_id, status),
        )
        changed = cur.rowcount or 0
    if changed and status in ("failed", "expired"):
        audit(
            db,
            "command",
            f"result_{status}",
            actor=worker_id,
            detail={"command_id": command_id, "error_code": error_code},
        )
    return bool(changed)


def command_status(db: Database, command_id: str) -> dict[str, Any] | None:
    row = db.query_one(
        "SELECT command_id, worker_id, command_type, created_at, expires_at, delivered_at, status, error_code "
        "FROM commands WHERE command_id = ?",
        (command_id,),
    )
    if row is None:
        return None
    return dict(row)
