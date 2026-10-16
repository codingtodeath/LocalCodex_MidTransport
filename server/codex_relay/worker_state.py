"""Worker 在线状态：有效心跳 + 反向通道 Health + instance 一致性。

见 01_DEVELOPMENT_SPEC.md 第 8 节。核心规则：
* 在线判定需要**心跳与隧道探测同时成立**，并且 instance 一致；
  不能只看一个“在线”圆点。
* 重复 seq 可以幂等确认，但**不得刷新在线时间**，也不得覆盖新快照。
* 服务器重启后不把数据库里的“在线”直接恢复为在线，先置 unknown。
* 分项状态（adapter / auth / quota）独立展示，quota 不决定“电脑是否在线”。
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import Any, Literal

from .db import Database, iso, parse_iso, utc_now

WorkerStateName = Literal["unknown", "online", "degraded", "offline"]

STATE_UNKNOWN = "unknown"
STATE_ONLINE = "online"
STATE_DEGRADED = "degraded"
STATE_OFFLINE = "offline"


@dataclass
class WorkerSnapshot:
    worker_id: str
    display_name: str | None = None
    state: WorkerStateName = STATE_UNKNOWN
    current_instance_id: str | None = None
    heartbeat_instance_id: str | None = None
    last_seen_at: str | None = None
    heartbeat_age_seconds: float | None = None
    heartbeat_fresh: bool = False
    probe_ok: bool | None = None
    probe_at: str | None = None
    probe_instance_id: str | None = None
    probe_fresh: bool = False
    instance_consistent: bool = False
    adapter_status: str | None = None
    adapter_ready: bool | None = None
    auth_state: str | None = None
    bridge_ready: bool | None = None
    active_requests: int | None = None
    last_inference_success_at: str | None = None
    capabilities: dict[str, Any] = field(default_factory=dict)
    account_epoch: str | None = None
    reasons: list[str] = field(default_factory=list)

    def as_dict(self) -> dict[str, Any]:
        return {
            "worker_id": self.worker_id,
            "display_name": self.display_name,
            "state": self.state,
            "current_instance_id": self.current_instance_id,
            "heartbeat_instance_id": self.heartbeat_instance_id,
            "last_seen_at": self.last_seen_at,
            "heartbeat_age_seconds": self.heartbeat_age_seconds,
            "heartbeat_fresh": self.heartbeat_fresh,
            "probe_ok": self.probe_ok,
            "probe_at": self.probe_at,
            "probe_instance_id": self.probe_instance_id,
            "probe_fresh": self.probe_fresh,
            "instance_consistent": self.instance_consistent,
            "adapter_status": self.adapter_status,
            "adapter_ready": self.adapter_ready,
            "auth_state": self.auth_state,
            "bridge_ready": self.bridge_ready,
            "active_requests": self.active_requests,
            "last_inference_success_at": self.last_inference_success_at,
            "capabilities": self.capabilities,
            "account_epoch": self.account_epoch,
            "reasons": self.reasons,
        }


def reset_after_restart(db: Database) -> None:
    """Gateway 启动时调用：历史“在线”不能直接当作在线。"""

    now = utc_now()
    with db.write() as conn:
        conn.execute(
            "UPDATE workers SET state = ?, probe_ok = NULL, probe_at = NULL, "
            "probe_instance_id = NULL, last_seen_at = NULL, updated_at = ?",
            (STATE_UNKNOWN, now),
        )


def ensure_worker(db: Database, worker_id: str, display_name: str | None = None) -> None:
    now = utc_now()
    with db.write() as conn:
        conn.execute(
            "INSERT INTO workers (worker_id, display_name, state, last_seq, created_at, updated_at) "
            "VALUES (?,?,?,0,?,?) ON CONFLICT(worker_id) DO NOTHING",
            (worker_id, display_name, STATE_UNKNOWN, now, now),
        )
        if display_name:
            conn.execute(
                "UPDATE workers SET display_name = ? WHERE worker_id = ? AND (display_name IS NULL OR display_name = '')",
                (display_name, worker_id),
            )


# --- 心跳 ---------------------------------------------------------------


@dataclass
class HeartbeatOutcome:
    accepted: bool
    duplicate: bool
    ack_seq: int
    reason: str | None = None
    instance_is_current: bool = True


def record_heartbeat(
    db: Database,
    *,
    worker_id: str,
    instance_id: str,
    seq: int,
    sent_at: str,
    capabilities: dict[str, Any],
    local: dict[str, Any],
    now: datetime | None = None,
) -> HeartbeatOutcome:
    """记录一次心跳。

    去重与实例语义（Q10）：
    * 未知实例            -> 接受；分配递增的 instance_seq 并成为当前实例。
    * instance_seq 更大   -> 新实例启动，接受并成为当前实例。
    * 就是当前实例且 seq 严格前进 -> 接受，刷新 last_seen。
    * 就是当前实例且 seq 相同     -> 重复包：幂等 ack，**不**刷新 last_seen。
    * 就是当前实例且 seq 更小     -> 乱序旧包：不刷新 last_seen、不覆盖状态。
    * instance_seq 更小的旧实例   -> 迟到的旧包：不刷新 last_seen、不覆盖新实例。
    """

    now = now or datetime.now(UTC)
    now_text = iso(now)

    ensure_worker(db, worker_id)

    with db.write() as conn:
        worker = conn.execute(
            "SELECT current_instance_id FROM workers WHERE worker_id = ?", (worker_id,)
        ).fetchone()
        current_instance = worker["current_instance_id"] if worker else None

        inst = conn.execute(
            "SELECT last_seq, instance_seq FROM worker_instances WHERE worker_id = ? AND instance_id = ?",
            (worker_id, instance_id),
        ).fetchone()

        if inst is None:
            # 新实例：分配新的 instance_seq（同 worker 内单调递增）。
            max_row = conn.execute(
                "SELECT COALESCE(MAX(instance_seq), 0) AS m FROM worker_instances WHERE worker_id = ?",
                (worker_id,),
            ).fetchone()
            instance_seq = int(max_row["m"]) + 1
            conn.execute(
                "INSERT INTO worker_instances (worker_id, instance_id, first_seen_at, last_seen_at, last_seq, instance_seq) "
                "VALUES (?,?,?,?,?,?)",
                (worker_id, instance_id, now_text, now_text, seq, instance_seq),
            )
            instance_last_seq: int | None = None
        else:
            instance_seq = int(inst["instance_seq"])
            instance_last_seq = int(inst["last_seq"])

        current_row = None
        if current_instance is not None:
            current_row = conn.execute(
                "SELECT instance_seq FROM worker_instances WHERE worker_id = ? AND instance_id = ?",
                (worker_id, current_instance),
            ).fetchone()

        reasons: list[str] = []
        if current_instance is None:
            # 首次见到任何实例：直接接受。
            accepted = True
        elif current_row is not None and instance_seq < int(current_row["instance_seq"]):
            # 旧实例的迟到包：不刷新在线状态，也不覆盖新实例的本地状态。
            accepted = False
            reasons.append("heartbeat_from_superseded_instance")
        elif instance_last_seq is not None and seq == instance_last_seq:
            accepted = False
            reasons.append("duplicate_seq")
        elif instance_last_seq is not None and seq < instance_last_seq:
            accepted = False
            reasons.append("stale_seq")
        else:
            accepted = True

        if inst is not None and accepted and instance_last_seq is not None and seq > instance_last_seq:
            conn.execute(
                "UPDATE worker_instances SET last_seen_at = ?, last_seq = ? "
                "WHERE worker_id = ? AND instance_id = ?",
                (now_text, seq, worker_id, instance_id),
            )

        if accepted:
            # 只更新事实数据。状态由 evaluate() 综合心跳 + 隧道探测决定，
            # 不在心跳路径上直接写 online，避免“只看心跳就全绿”。
            conn.execute(
                "UPDATE workers SET current_instance_id = ?, last_seen_at = ?, last_seq = ?, "
                "capabilities_json = ?, local_json = ?, updated_at = ? "
                "WHERE worker_id = ?",
                (
                    instance_id,
                    now_text,
                    seq,
                    json.dumps(capabilities, ensure_ascii=False),
                    json.dumps(local, ensure_ascii=False),
                    now_text,
                    worker_id,
                ),
            )

    return HeartbeatOutcome(
        accepted=accepted,
        duplicate=("duplicate_seq" in reasons),
        ack_seq=seq,
        reason=";".join(reasons) if reasons else None,
        instance_is_current=("heartbeat_from_superseded_instance" not in reasons),
    )


# --- 隧道探测 -----------------------------------------------------------


def record_probe(
    db: Database,
    worker_id: str,
    *,
    ok: bool,
    instance_id: str | None = None,
    now: datetime | None = None,
) -> None:
    now = now or datetime.now(UTC)
    with db.write() as conn:
        ensure_worker(db, worker_id)
        conn.execute(
            "UPDATE workers SET probe_ok = ?, probe_at = ?, probe_instance_id = ?, updated_at = ? "
            "WHERE worker_id = ?",
            (1 if ok else 0, iso(now), instance_id if ok else None, iso(now), worker_id),
        )


def should_probe(db: Database, worker_id: str, interval_seconds: int, now: datetime | None = None) -> bool:
    """限流探测：不必每次请求都探测，避免把隧道探测变成负载。"""

    now = now or datetime.now(UTC)
    row = db.query_one("SELECT probe_at FROM workers WHERE worker_id = ?", (worker_id,))
    if row is None or not row["probe_at"]:
        return True
    last = parse_iso(row["probe_at"])
    if last is None:
        return True
    return (now - last).total_seconds() >= interval_seconds


# --- 状态评估 -----------------------------------------------------------


def evaluate(
    db: Database,
    worker_id: str,
    config: Any,
    *,
    now: datetime | None = None,
) -> WorkerSnapshot:
    """综合心跳与探测，给出分项状态。"""

    now = now or datetime.now(UTC)
    row = db.query_one("SELECT * FROM workers WHERE worker_id = ?", (worker_id,))
    if row is None:
        return WorkerSnapshot(worker_id=worker_id, state=STATE_UNKNOWN, reasons=["worker_never_seen"])

    snap = WorkerSnapshot(
        worker_id=worker_id,
        display_name=row["display_name"],
        current_instance_id=row["current_instance_id"],
        heartbeat_instance_id=row["current_instance_id"],
        last_seen_at=row["last_seen_at"],
        probe_ok=None if row["probe_ok"] is None else bool(row["probe_ok"]),
        probe_at=row["probe_at"],
        probe_instance_id=row["probe_instance_id"],
        account_epoch=row["account_epoch"],
    )

    local = _safe_json(row["local_json"])
    caps = _safe_json(row["capabilities_json"])
    if isinstance(local, dict):
        snap.adapter_status = local.get("adapter_status")
        snap.adapter_ready = local.get("adapter_ready")
        snap.auth_state = local.get("auth_state")
        snap.bridge_ready = local.get("bridge_ready")
        active = local.get("active_requests")
        snap.active_requests = active if isinstance(active, int) else None
        snap.last_inference_success_at = local.get("last_inference_success_at")
    if isinstance(caps, dict):
        snap.capabilities = caps

    last_seen = parse_iso(row["last_seen_at"])
    if last_seen is None:
        snap.heartbeat_fresh = False
        snap.heartbeat_age_seconds = None
        snap.reasons.append("no_heartbeat")
    else:
        age = (now - last_seen).total_seconds()
        snap.heartbeat_age_seconds = round(age, 3)
        # 明显未来时间：时钟偏差，不能算作“新鲜”。
        if age < -60:
            snap.heartbeat_fresh = False
            snap.reasons.append("heartbeat_clock_skew_future")
        else:
            snap.heartbeat_fresh = age <= config.heartbeat_stale_seconds

    probe_at = parse_iso(row["probe_at"])
    if probe_at is None:
        snap.probe_fresh = False
    else:
        snap.probe_fresh = 0 <= (now - probe_at).total_seconds() <= config.probe_interval_seconds * 3

    # instance 一致性：探测成功且观测到的 instance 与心跳当前实例相同。
    snap.instance_consistent = bool(
        snap.probe_ok
        and row["probe_instance_id"] is not None
        and row["probe_instance_id"] == row["current_instance_id"]
    )

    if snap.heartbeat_fresh and snap.instance_consistent and snap.probe_fresh:
        snap.state = STATE_ONLINE
    elif snap.heartbeat_fresh:
        # 心跳正常但隧道断/实例不一致
        snap.state = STATE_DEGRADED
        if snap.probe_ok and not snap.instance_consistent:
            snap.reasons.append("probe_instance_mismatch")
        else:
            snap.reasons.append("tunnel_probe_not_confirmed")
    elif not snap.heartbeat_fresh and snap.instance_consistent and snap.probe_fresh:
        # 隧道正常但心跳停：不能显示全绿。
        snap.state = STATE_DEGRADED
        snap.reasons.append("heartbeat_stale_but_probe_ok")
    else:
        if last_seen is None and probe_at is None:
            snap.state = STATE_UNKNOWN
        else:
            snap.state = STATE_OFFLINE
            snap.reasons.append("no_fresh_heartbeat")

    # adapter 未就绪也是 degraded，而不是 online。
    if snap.state == STATE_ONLINE and snap.adapter_ready is False:
        snap.state = STATE_DEGRADED
        snap.reasons.append("adapter_not_ready")

    return snap


def _safe_json(text: str | None) -> Any:
    if not text:
        return None
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        return None


def is_servable(snapshot: WorkerSnapshot) -> tuple[bool, str | None]:
    """是否允许把请求转发到该 worker。返回 (可服务, 拒绝原因)。"""

    if snapshot.state == STATE_OFFLINE:
        return False, "worker_unavailable"
    if snapshot.state == STATE_UNKNOWN:
        return False, "worker_state_unknown"
    if not (snapshot.heartbeat_fresh and snapshot.probe_fresh and snapshot.instance_consistent):
        return False, "worker_unavailable"
    if snapshot.adapter_ready is False:
        return False, "adapter_unavailable"
    if snapshot.auth_state == "reauth_required":
        return False, "upstream_reauth_required"
    return True, None


def mark_worker_state(db: Database, worker_id: str, state: str) -> None:
    db.execute(
        "UPDATE workers SET state = ?, updated_at = ? WHERE worker_id = ?",
        (state, utc_now(), worker_id),
    )


def heartbeat_age(worker_id: str, last_seen_at: str | None, now: datetime | None = None) -> float | None:
    if last_seen_at is None:
        return None
    parsed = parse_iso(last_seen_at)
    if parsed is None:
        return None
    return ((now or datetime.now(UTC)) - parsed).total_seconds()


def stale_threshold(config: Any, now: datetime | None = None) -> str:
    now = now or datetime.now(UTC)
    return iso(now - timedelta(seconds=config.heartbeat_stale_seconds))
