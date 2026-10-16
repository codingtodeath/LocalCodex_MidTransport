"""额度快照存储。

见 01_DEVELOPMENT_SPEC.md 7.4 / 7.5、第 9 节。
关键语义：
* 「最近成功快照」与「最近查询状态」**分开保存**。
* quota=null 表示本次心跳没有新的查询结果，**不清空**旧快照。
* quota.status 非 ok 时只记录失败原因，不用空的 windows 覆盖成功数据。
* captured_at 只在实际取得新额度时更新；缓存重复上报不改时间。
* captured_at 为 null 不能被解释成当前时间。
* 到达旧快照的 reset 时间但 Windows 离线时，**不得**自动把进度条改成剩余 100%。
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

from .db import Database, iso, parse_iso, utc_now
from .models import Quota

SUCCESS_STATUSES = ("ok",)


@dataclass
class QuotaView:
    """网站展示所需的额度视图。"""

    has_snapshot: bool = False
    status: str = "unavailable"
    source: str | None = None
    account_alias: str | None = None
    identity_check: str | None = None
    plan_type: str | None = None
    captured_at: str | None = None
    received_at: str | None = None
    age_seconds: float | None = None
    freshness: str = "unavailable"  # fresh | stale | unavailable
    windows: list[dict[str, Any]] = None  # type: ignore[assignment]
    credits: dict[str, Any] | None = None
    error_code: str | None = None
    last_attempt_at: str | None = None
    last_attempt_category: str | None = None
    last_attempt_error: str | None = None
    account_epoch: str | None = None
    reset_pending_confirmation: bool = False
    notes: list[str] = None  # type: ignore[assignment]

    def __post_init__(self) -> None:
        if self.windows is None:
            self.windows = []
        if self.notes is None:
            self.notes = []

    def as_dict(self) -> dict[str, Any]:
        return {
            "has_snapshot": self.has_snapshot,
            "status": self.status,
            "source": self.source,
            "account_alias": self.account_alias,
            "identity_check": self.identity_check,
            "plan_type": self.plan_type,
            "captured_at": self.captured_at,
            "received_at": self.received_at,
            "age_seconds": self.age_seconds,
            "freshness": self.freshness,
            "windows": self.windows,
            "credits": self.credits,
            "error_code": self.error_code,
            "last_attempt_at": self.last_attempt_at,
            "last_attempt_category": self.last_attempt_category,
            "last_attempt_error": self.last_attempt_error,
            "account_epoch": self.account_epoch,
            "reset_pending_confirmation": self.reset_pending_confirmation,
            "notes": self.notes,
        }


def record_quota_attempt(
    db: Database,
    *,
    worker_id: str,
    at: str,
    result_category: str,
    error_code: str | None = None,
    account_epoch: str | None = None,
) -> None:
    db.execute(
        "INSERT INTO quota_attempts (worker_id, at, result_category, error_code, account_epoch) "
        "VALUES (?,?,?,?,?)",
        (worker_id, at, result_category, error_code, account_epoch),
    )


def record_quota(db: Database, worker_id: str, quota: Quota | None, *, received_at: str | None = None) -> str:
    """处理一次心跳里的 quota 字段。返回处理结论（便于测试与日志）。"""

    received_at = received_at or utc_now()

    if quota is None:
        # 本次心跳没有新的查询结果：不清空旧快照，也不记录失败尝试。
        return "absent"

    record_quota_attempt(
        db,
        worker_id=worker_id,
        at=quota.last_attempt_at,
        result_category=quota.status,
        error_code=quota.error_code,
        account_epoch=quota.account_epoch,
    )

    if quota.status not in SUCCESS_STATUSES:
        # 失败对象里的空 windows 绝不覆盖之前的成功数据。
        return f"failure:{quota.status}"

    windows = [w.model_dump() for w in quota.windows]
    credits = quota.credits.model_dump() if quota.credits is not None else None

    with db.write() as conn:
        # 账号/工作区变化会更换 epoch：旧快照不再“当前”，但不被删除。
        conn.execute(
            "UPDATE quota_snapshots SET is_current = 0 WHERE worker_id = ? AND is_current = 1 "
            "AND COALESCE(account_epoch, '') <> COALESCE(?, '')",
            (worker_id, quota.account_epoch),
        )
        # 同一 epoch 的重复上报（captured_at 未变）不新增行。
        existing = conn.execute(
            "SELECT snapshot_id, captured_at FROM quota_snapshots "
            "WHERE worker_id = ? AND is_current = 1 AND COALESCE(account_epoch,'') = COALESCE(?,'') "
            "ORDER BY captured_at DESC LIMIT 1",
            (worker_id, quota.account_epoch),
        ).fetchone()

        if existing is not None and existing["captured_at"] == quota.captured_at:
            # 缓存数据随心跳重复上报：不更新 captured_at，只刷新接收时间。
            conn.execute(
                "UPDATE quota_snapshots SET received_at = ?, windows_json = ?, credits_json = ?, "
                "account_alias = ?, identity_check = ?, plan_type = ?, status = ?, source = ?, "
                "error_code = ? WHERE snapshot_id = ?",
                (
                    received_at,
                    json.dumps(windows, ensure_ascii=False),
                    json.dumps(credits, ensure_ascii=False) if credits is not None else None,
                    quota.account_alias,
                    quota.identity_check,
                    quota.plan_type,
                    quota.status,
                    quota.source,
                    quota.error_code,
                    existing["snapshot_id"],
                ),
            )
            result = "refreshed_same_capture"
        else:
            if existing is not None:
                conn.execute(
                    "UPDATE quota_snapshots SET is_current = 0 WHERE snapshot_id = ?",
                    (existing["snapshot_id"],),
                )
            conn.execute(
                "INSERT INTO quota_snapshots (worker_id, account_epoch, account_alias, identity_check, "
                "plan_type, status, source, captured_at, received_at, windows_json, credits_json, "
                "error_code, is_current) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,1)",
                (
                    worker_id,
                    quota.account_epoch,
                    quota.account_alias,
                    quota.identity_check,
                    quota.plan_type,
                    quota.status,
                    quota.source,
                    quota.captured_at,
                    received_at,
                    json.dumps(windows, ensure_ascii=False),
                    json.dumps(credits, ensure_ascii=False) if credits is not None else None,
                    quota.error_code,
                ),
            )
            result = "new_snapshot"

        conn.execute(
            "UPDATE workers SET account_epoch = ?, updated_at = ? WHERE worker_id = ?",
            (quota.account_epoch, utc_now(), worker_id),
        )
    return result


def get_current_snapshot(db: Database, worker_id: str) -> dict[str, Any] | None:
    row = db.query_one(
        "SELECT * FROM quota_snapshots WHERE worker_id = ? AND is_current = 1 "
        "ORDER BY captured_at DESC LIMIT 1",
        (worker_id,),
    )
    if row is None:
        return None
    return dict(row)


def record_account_usage(db: Database, worker_id: str, usage: Any) -> None:
    """保存账号级汇总（可选，口径与本网关流水不同，页面不能相加）。"""

    import json as _json

    payload = usage.model_dump() if hasattr(usage, "model_dump") else usage
    db.execute(
        "INSERT INTO account_usage (worker_id, captured_at, received_at, summary_json, daily_json) "
        "VALUES (?,?,?,?,?)",
        (
            worker_id,
            payload.get("captured_at"),
            utc_now(),
            _json.dumps(payload.get("summary"), ensure_ascii=False),
            _json.dumps(payload.get("daily_buckets"), ensure_ascii=False)
            if payload.get("daily_buckets") is not None
            else None,
        ),
    )


def get_account_usage(db: Database, worker_id: str) -> dict[str, Any] | None:
    row = db.query_one(
        "SELECT captured_at, received_at, summary_json, daily_json FROM account_usage "
        "WHERE worker_id = ? ORDER BY captured_at DESC LIMIT 1",
        (worker_id,),
    )
    if row is None:
        return None
    import json as _json

    try:
        summary = _json.loads(row["summary_json"] or "{}")
    except _json.JSONDecodeError:
        summary = {}
    daily = None
    if row["daily_json"]:
        try:
            daily = _json.loads(row["daily_json"])
        except _json.JSONDecodeError:
            daily = None
    return {
        "captured_at": row["captured_at"],
        "received_at": row["received_at"],
        "scope": "upstream_account_summary",
        "summary": summary,
        "daily_buckets": daily,
    }


def get_last_attempt(db: Database, worker_id: str) -> dict[str, Any] | None:
    row = db.query_one(
        "SELECT * FROM quota_attempts WHERE worker_id = ? ORDER BY at DESC, attempt_id DESC LIMIT 1",
        (worker_id,),
    )
    if row is None:
        return None
    return dict(row)


def build_view(
    db: Database,
    worker_id: str,
    config: Any,
    *,
    worker_online: bool,
    now: datetime | None = None,
) -> QuotaView:
    """构造额度视图，包含 freshness、stale 与 reset 待确认标记。"""

    now = now or datetime.now(UTC)
    view = QuotaView()
    snapshot = get_current_snapshot(db, worker_id)
    attempt = get_last_attempt(db, worker_id)

    if attempt is not None:
        view.last_attempt_at = attempt["at"]
        view.last_attempt_category = attempt["result_category"]
        view.last_attempt_error = attempt["error_code"]

    if snapshot is None:
        view.status = "unavailable"
        view.notes.append("尚未收到任何成功额度快照。")
        return view

    view.has_snapshot = True
    view.status = snapshot["status"]
    view.source = snapshot["source"]
    view.account_alias = snapshot["account_alias"]
    view.identity_check = snapshot["identity_check"]
    view.plan_type = snapshot["plan_type"]
    view.captured_at = snapshot["captured_at"]
    view.received_at = snapshot["received_at"]
    view.error_code = snapshot["error_code"]
    view.account_epoch = snapshot["account_epoch"]
    try:
        view.windows = json.loads(snapshot["windows_json"] or "[]")
    except json.JSONDecodeError:
        view.windows = []
        view.notes.append("窗口数据损坏，无法展示。")
    if snapshot["credits_json"]:
        try:
            view.credits = json.loads(snapshot["credits_json"])
        except json.JSONDecodeError:
            view.credits = None

    captured = parse_iso(snapshot["captured_at"])
    if captured is None:
        # captured_at 为 null 不能解释成当前时间。
        view.freshness = "unavailable"
        view.notes.append("快照缺少采集时间，无法判断新鲜度。")
    else:
        age = (now - captured).total_seconds()
        view.age_seconds = round(age, 3)
        if age < -60:
            view.freshness = "stale"
            view.notes.append("快照时间明显位于未来，存在时钟偏差。")
        elif age > config.quota_stale_seconds:
            view.freshness = "stale"
            view.notes.append(
                f"额度数据已超过 {config.quota_stale_seconds} 秒未更新，仅供参考。"
            )
        else:
            view.freshness = "fresh"

    # 身份未确认 / 不匹配：不能当作已确认的账号额度展示。
    if view.identity_check in ("unverified", "mismatch", None):
        view.notes.append("账号身份未确认或与代理不一致，额度仅供参考。")

    # 到达 reset 时间但电脑离线：明确提示等待联网确认，绝不自行改成剩余 100%。
    if not worker_online:
        pending = False
        for window in view.windows:
            resets_at = parse_iso(window.get("resets_at"))
            if resets_at is not None and resets_at <= now:
                pending = True
                break
        if pending:
            view.reset_pending_confirmation = True
            view.notes.append("预计已到重置时间，但 Windows 当前离线，等待联网确认；不显示剩余 100%。")

    return view
