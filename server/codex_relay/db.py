"""SQLite 存储层。

约束（见 01_DEVELOPMENT_SPEC.md 第 13 节）：
* WAL、busy_timeout、短事务。
* 单个 Uvicorn worker，但并发限制仍有明确锁（应用层信号量）。
* 计量写入失败不能静默吞掉；调用方必须把它转成 503 metering_unavailable。
* 备份/清理不能阻塞长流。
"""

from __future__ import annotations

import json
import sqlite3
import threading
from collections.abc import Iterable, Iterator
from contextlib import contextmanager
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

SCHEMA_VERSION = 1
MIGRATIONS_DIR = Path(__file__).parent / "migrations"


class DatabaseError(Exception):
    pass


def _split_statements(script: str) -> list[str]:
    """把迁移脚本切成单条语句。

    只用 `sqlite3.complete_statement` 逐行累积，因此行内分号、字符串字面量与
    `--` 注释都不会被误切。
    """

    statements: list[str] = []
    buffer = ""
    for line in script.splitlines(keepends=True):
        buffer += line
        if buffer.strip() and sqlite3.complete_statement(buffer):
            statement = buffer.strip()
            if statement:
                statements.append(statement)
            buffer = ""
    if buffer.strip():
        # 末尾没有分号也接受，便于手写迁移。
        statements.append(buffer.strip())
    return statements


def utc_now() -> str:
    """统一 UTC ISO-8601，带 Z 后缀（契约要求实际输出为 Z 形式）。"""

    return datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%S.%f")[:-3] + "Z"


def iso(value: datetime) -> str:
    if value.tzinfo is None:
        value = value.replace(tzinfo=UTC)
    return value.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%S.%f")[:-3] + "Z"


def parse_iso(value: str | None) -> datetime | None:
    if not value:
        return None
    text = value.strip()
    if text.endswith("Z"):
        text = text[:-1] + "+00:00"
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=UTC)
    return parsed.astimezone(UTC)


class Database:
    """薄封装。每个线程一个连接；应用只使用单 worker + 线程池。"""

    def __init__(self, path: str | Path) -> None:
        self.path = Path(path)
        self._local = threading.local()
        self._write_lock = threading.RLock()
        self._closed = False

    # --- 连接 ---------------------------------------------------------

    def connect(self) -> sqlite3.Connection:
        conn = getattr(self._local, "conn", None)
        if conn is None:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            conn = sqlite3.connect(
                self.path,
                timeout=10.0,
                isolation_level=None,  # 显式事务
                check_same_thread=False,
            )
            conn.row_factory = sqlite3.Row
            conn.execute("PRAGMA journal_mode=WAL")
            conn.execute("PRAGMA busy_timeout=10000")
            conn.execute("PRAGMA foreign_keys=ON")
            conn.execute("PRAGMA synchronous=NORMAL")
            self._local.conn = conn
        return conn

    @contextmanager
    def write(self) -> Iterator[sqlite3.Connection]:
        """写事务。失败时回滚并抛出，绝不静默继续。

        可重入：内层 `write()` 复用外层事务（同一个连接），只有最外层负责
        COMMIT / ROLLBACK。否则嵌套调用会触发
        `cannot start a transaction within a transaction`。
        """

        if self._closed:
            raise DatabaseError("database is closed")
        conn = self.connect()
        depth = getattr(self._local, "depth", 0)

        if depth > 0:
            # 已在外层事务中：直接复用，内层不提交也不回滚。
            self._local.depth = depth + 1
            try:
                yield conn
            finally:
                self._local.depth = depth
            return

        with self._write_lock:
            try:
                conn.execute("BEGIN IMMEDIATE")
            except sqlite3.Error as exc:
                raise DatabaseError(f"begin failed: {exc}") from exc
            self._local.depth = 1
            try:
                yield conn
            except Exception:
                try:
                    conn.execute("ROLLBACK")
                except sqlite3.Error:
                    pass
                self._local.depth = 0
                raise
            else:
                try:
                    conn.execute("COMMIT")
                finally:
                    self._local.depth = 0

    def query(self, sql: str, params: Iterable[Any] = ()) -> list[sqlite3.Row]:
        conn = self.connect()
        return list(conn.execute(sql, tuple(params)))

    def query_one(self, sql: str, params: Iterable[Any] = ()) -> sqlite3.Row | None:
        conn = self.connect()
        return conn.execute(sql, tuple(params)).fetchone()

    def execute(self, sql: str, params: Iterable[Any] = ()) -> None:
        with self.write() as conn:
            conn.execute(sql, tuple(params))

    def executemany(self, sql: str, rows: Iterable[Iterable[Any]]) -> None:
        with self.write() as conn:
            conn.executemany(sql, [tuple(r) for r in rows])

    # --- 迁移 ---------------------------------------------------------

    def migrate(self) -> int:
        """按文件顺序应用迁移。

        注意：不能使用 `executescript`，它会隐式提交并破坏显式事务
        （`cannot rollback - no transaction is active`）。这里逐条语句执行，
        并在同一个事务里记录版本。
        """

        conn = self.connect()
        conn.execute(
            "CREATE TABLE IF NOT EXISTS schema_migrations ("
            " version INTEGER PRIMARY KEY,"
            " name TEXT NOT NULL,"
            " applied_at TEXT NOT NULL)"
        )
        applied = {row["version"] for row in conn.execute("SELECT version FROM schema_migrations")}
        count = 0
        for sql_file in sorted(MIGRATIONS_DIR.glob("*.sql")):
            try:
                version = int(sql_file.name.split("_", 1)[0])
            except ValueError as exc:
                raise DatabaseError(f"迁移文件名无法解析版本：{sql_file.name}") from exc
            if version in applied:
                continue
            statements = _split_statements(sql_file.read_text(encoding="utf-8"))
            conn.execute("BEGIN IMMEDIATE")
            try:
                for statement in statements:
                    conn.execute(statement)
                conn.execute(
                    "INSERT INTO schema_migrations (version, name, applied_at) VALUES (?,?,?)",
                    (version, sql_file.name, utc_now()),
                )
                conn.execute("COMMIT")
            except sqlite3.Error as exc:
                try:
                    conn.execute("ROLLBACK")
                except sqlite3.Error:
                    # 事务可能已被 SQLite 自动回滚；保留原始错误。
                    pass
                raise DatabaseError(f"迁移 {sql_file.name} 失败：{exc}") from exc
            count += 1
        return count

    def integrity_ok(self) -> bool:
        row = self.query_one("PRAGMA quick_check")
        return bool(row) and row[0] == "ok"

    def close(self) -> None:
        self._closed = True
        conn = getattr(self._local, "conn", None)
        if conn is not None:
            conn.close()
            self._local.conn = None

    # --- 备份 ---------------------------------------------------------

    def backup_to(self, target: str | Path) -> Path:
        """SQLite 在线备份。不能只拷贝活跃 WAL 主文件。"""

        target_path = Path(target)
        target_path.parent.mkdir(parents=True, exist_ok=True)
        src = self.connect()
        dest = sqlite3.connect(target_path)
        try:
            src.backup(dest)
        finally:
            dest.close()
        return target_path


# --- 保留期清理 ---------------------------------------------------------


def cleanup(db: Database, config: Any, now: datetime | None = None) -> dict[str, int]:
    """按规格第 13 节的保留期清理。返回各表删除行数。"""

    now = now or datetime.now(UTC)
    removed: dict[str, int] = {}

    def cutoff(days: int) -> str:
        return iso(now - timedelta(days=days))

    with db.write() as conn:
        cur = conn.execute("DELETE FROM requests WHERE started_at < ?", (cutoff(config.retention_request_days),))
        removed["requests"] = cur.rowcount or 0
        cur = conn.execute("DELETE FROM daily_rollups WHERE day < ?", ((now - timedelta(days=config.retention_rollup_days)).strftime("%Y-%m-%d"),))
        removed["daily_rollups"] = cur.rowcount or 0
        # 只删除非当前快照，最新成功快照始终保留。
        cur = conn.execute(
            "DELETE FROM quota_snapshots WHERE is_current = 0 AND received_at < ?",
            (cutoff(config.retention_quota_days),),
        )
        removed["quota_snapshots"] = cur.rowcount or 0
        cur = conn.execute("DELETE FROM quota_attempts WHERE at < ?", (cutoff(config.retention_quota_days),))
        removed["quota_attempts"] = cur.rowcount or 0
        cur = conn.execute("DELETE FROM commands WHERE created_at < ?", (cutoff(config.retention_command_days),))
        removed["commands"] = cur.rowcount or 0
        cur = conn.execute("DELETE FROM audit_events WHERE at < ?", (cutoff(config.retention_command_days),))
        removed["audit_events"] = cur.rowcount or 0
        cur = conn.execute("DELETE FROM login_attempts WHERE at < ?", (cutoff(1),))
        removed["login_attempts"] = cur.rowcount or 0
    return removed


def audit(
    db: Database,
    category: str,
    action: str,
    *,
    actor: str | None = None,
    detail: dict[str, Any] | None = None,
) -> None:
    """写审计事件。detail 必须已脱敏；此函数不做内容判断。"""

    db.execute(
        "INSERT INTO audit_events (at, category, action, actor, detail_json) VALUES (?,?,?,?,?)",
        (utc_now(), category, action, actor, json.dumps(detail, ensure_ascii=False) if detail else None),
    )
