"""鉴权与秘密管理。

角色分离（见 01_DEVELOPMENT_SPEC.md 第 5、12 节）：
* `gw_...`  客户端网关 Key —— 只能访问 /v1/*
* `tel_...` Windows 遥测 Key —— 只能访问 /internal/v1/telemetry
* `wkr_...` Gateway -> Bridge Key —— 只用于出站调用 Bridge 的三个白名单端点
* 管理员用户名/密码 + 服务端会话 Cookie —— 只能访问 /admin/api/*

规则：
* 高熵随机 Key 用 HMAC-SHA256 + 服务器 pepper 校验，DB 只存摘要。
* 密码用 PBKDF2-HMAC-SHA256（高迭代）加随机盐；不在任何地方保存明文。
* Cookie 不能代替模型 Key；Key 也不能登录后台。
* 所有比较使用常数时间函数。
"""

from __future__ import annotations

import hashlib
import hmac
import json
import os
import secrets
from dataclasses import dataclass
from pathlib import Path

from .db import Database, audit, iso, utc_now
from datetime import UTC, datetime, timedelta

KEY_BYTES = 32  # 至少 32 字节随机熵
PBKDF2_ITERATIONS = 600_000

ROLE_CLIENT = "client"
ROLE_TELEMETRY = "telemetry"
ROLE_WORKER = "worker"

PREFIXES = {
    ROLE_CLIENT: "gw",
    ROLE_TELEMETRY: "tel",
    ROLE_WORKER: "wkr",
}


class SecretStoreError(Exception):
    pass


def generate_key(role: str) -> str:
    """生成 `<prefix>_<43字符base64url>` 形式的高熵 Key。"""

    prefix = PREFIXES[role]
    return f"{prefix}_{secrets.token_urlsafe(KEY_BYTES)}"


def read_secret_file(path: str | Path, *, create: bool = False, length: int = 32) -> bytes:
    """读取二进制秘密文件；create=True 时以 0600 创建。

    允许的权限：0640 或更严格（属主可读写，属组可读，其他无权限）。
    这样服务用户可以通过属组读取秘密，而秘密仍不对其他用户开放。
    明确拒绝属组可写、其他用户有任何权限，以及 0644 / 0666 之类。
    目录本身应为 0750 root:<service-group>。
    """

    p = Path(path)
    if not p.exists():
        if not create:
            raise SecretStoreError(f"秘密文件不存在：{p}")
        p.parent.mkdir(parents=True, exist_ok=True)
        # 以 0600 创建，避免任何竞态窗口。
        fd = os.open(p, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        try:
            os.write(fd, secrets.token_bytes(length))
        finally:
            os.close(fd)
    status = p.stat()
    mode = status.st_mode & 0o777
    if mode & 0o007:
        raise SecretStoreError(
            f"秘密文件对其他用户开放（{oct(mode)}）：{p}；应为 0640 或更严格"
        )
    if mode & 0o020:
        raise SecretStoreError(
            f"秘密文件属组可写（{oct(mode)}）：{p}；属组不应具有写权限"
        )
    data = p.read_bytes()
    if len(data) < 16:
        raise SecretStoreError(f"秘密文件内容过短：{p}")
    return data


def read_secret_text(path: str | Path) -> str:
    p = Path(path)
    data = read_secret_file(p)
    return data.decode("utf-8").strip()


class Pepper:
    """用于 Key 摘要的服务器 pepper。"""

    def __init__(self, secret: bytes) -> None:
        self._secret = secret

    def digest(self, key: str) -> str:
        return hmac.new(self._secret, key.encode("utf-8"), hashlib.sha256).hexdigest()

    def matches(self, key: str, digest: str) -> bool:
        return hmac.compare_digest(self.digest(key), digest)


def session_hash(token: str, secret: bytes) -> str:
    return hmac.new(secret, token.encode("utf-8"), hashlib.sha256).hexdigest()


# --- 密码哈希 -----------------------------------------------------------


def hash_password(password: str, *, iterations: int = PBKDF2_ITERATIONS) -> str:
    salt = secrets.token_bytes(16)
    derived = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), salt, iterations)
    return f"pbkdf2_sha256${iterations}${salt.hex()}${derived.hex()}"


def verify_password(password: str, encoded: str) -> bool:
    try:
        algorithm, iter_text, salt_hex, hash_hex = encoded.split("$")
    except ValueError:
        return False
    if algorithm != "pbkdf2_sha256":
        return False
    try:
        iterations = int(iter_text)
        salt = bytes.fromhex(salt_hex)
        expected = bytes.fromhex(hash_hex)
    except ValueError:
        return False
    if iterations < 100_000:
        # 拒绝过弱的参数，避免被降级攻击。
        return False
    derived = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), salt, iterations)
    return hmac.compare_digest(derived, expected)


# --- 客户端 Key ---------------------------------------------------------


@dataclass(frozen=True)
class ClientKey:
    key_id: str
    alias: str
    digest: str
    created_at: str
    revoked_at: str | None


def create_client_key(db: Database, pepper: Pepper, alias: str) -> tuple[str, ClientKey]:
    """创建客户端 Key。明文只在此处返回一次，DB 只保存摘要。"""

    key_id = f"ck_{secrets.token_hex(8)}"
    token = generate_key(ROLE_CLIENT)
    digest = pepper.digest(token)
    now = utc_now()
    db.execute(
        "INSERT INTO client_keys (key_id, alias, digest, created_at, revoked_at, last_used_at) "
        "VALUES (?,?,?,?,NULL,NULL)",
        (key_id, alias, digest, now),
    )
    audit(db, "client_key", "created", actor="admin", detail={"key_id": key_id, "alias": alias})
    return token, ClientKey(key_id, alias, digest, now, None)


def lookup_client_key(db: Database, pepper: Pepper, token: str) -> ClientKey | None:
    """按 digest 精确匹配。不做“模糊查找”，避免时序与枚举。"""

    digest = pepper.digest(token)
    row = db.query_one(
        "SELECT key_id, alias, digest, created_at, revoked_at FROM client_keys WHERE digest = ?",
        (digest,),
    )
    if row is None:
        return None
    if row["revoked_at"]:
        return None
    return ClientKey(row["key_id"], row["alias"], row["digest"], row["created_at"], row["revoked_at"])


def revoke_client_key(db: Database, key_id: str) -> bool:
    with db.write() as conn:
        cur = conn.execute(
            "UPDATE client_keys SET revoked_at = ? WHERE key_id = ? AND revoked_at IS NULL",
            (utc_now(), key_id),
        )
        changed = cur.rowcount or 0
    if changed:
        audit(db, "client_key", "revoked", actor="admin", detail={"key_id": key_id})
    return bool(changed)


def touch_client_key(db: Database, key_id: str) -> None:
    db.execute("UPDATE client_keys SET last_used_at = ? WHERE key_id = ?", (utc_now(), key_id))


# --- Worker / 遥测 Key --------------------------------------------------


def upsert_worker_credential(db: Database, worker_id: str, role: str, token: str, pepper: Pepper) -> None:
    if role not in PREFIXES:
        raise ValueError(f"未知角色：{role}")
    now = utc_now()
    with db.write() as conn:
        conn.execute(
            "INSERT INTO worker_credentials (worker_id, role, digest, created_at, updated_at) "
            "VALUES (?,?,?,?,?) "
            "ON CONFLICT(worker_id, role) DO UPDATE SET digest=excluded.digest, "
            "updated_at=excluded.updated_at, revoked_at=NULL",
            (worker_id, role, pepper.digest(token), now, now),
        )
    audit(db, "worker_credential", f"{role}_set", actor="admin", detail={"worker_id": worker_id})


def verify_worker_credential(db: Database, role: str, token: str, pepper: Pepper) -> str | None:
    """校验遥测/Worker Key，返回绑定的 worker_id。跨角色使用一律失败。"""

    if not token.startswith(PREFIXES[role] + "_"):
        return None
    digest = pepper.digest(token)
    row = db.query_one(
        "SELECT worker_id FROM worker_credentials WHERE role = ? AND digest = ? AND revoked_at IS NULL",
        (role, digest),
    )
    if row is None:
        return None
    return str(row["worker_id"])


def revoke_worker_credential(db: Database, worker_id: str, role: str) -> bool:
    with db.write() as conn:
        cur = conn.execute(
            "UPDATE worker_credentials SET revoked_at = ?, updated_at = ? "
            "WHERE worker_id = ? AND role = ? AND revoked_at IS NULL",
            (utc_now(), utc_now(), worker_id, role),
        )
        changed = cur.rowcount or 0
    if changed:
        audit(db, "worker_credential", f"{role}_revoked", actor="admin", detail={"worker_id": worker_id})
    return bool(changed)


# --- 管理员 -------------------------------------------------------------


def create_admin(db: Database, username: str, password: str) -> None:
    if len(password) < 12:
        raise ValueError("管理员密码至少 12 个字符")
    now = utc_now()
    admin_id = f"adm_{secrets.token_hex(6)}"
    with db.write() as conn:
        conn.execute(
            "INSERT INTO admin_credentials (admin_id, username, password_hash, created_at, updated_at) "
            "VALUES (?,?,?,?,?) "
            "ON CONFLICT(username) DO UPDATE SET password_hash=excluded.password_hash, "
            "updated_at=excluded.updated_at",
            (admin_id, username, hash_password(password), now, now),
        )
    audit(db, "admin", "password_set", actor=username)


def verify_admin(db: Database, username: str, password: str) -> bool:
    row = db.query_one("SELECT password_hash FROM admin_credentials WHERE username = ?", (username,))
    if row is None:
        # 即使用户不存在也做一次哈希，减小用户名枚举的时序差异。
        verify_password(password, "pbkdf2_sha256$600000$" + "00" * 16 + "$" + "00" * 32)
        return False
    return verify_password(password, row["password_hash"])


def login_rate_limited(db: Database, ip: str, limit_per_minute: int) -> bool:
    since = iso(datetime.now(UTC) - timedelta(minutes=1))
    row = db.query_one(
        "SELECT COUNT(*) AS n FROM login_attempts WHERE ip = ? AND at >= ? AND success = 0",
        (ip, since),
    )
    return bool(row) and int(row["n"]) >= limit_per_minute


def record_login_attempt(db: Database, ip: str, success: bool) -> None:
    db.execute(
        "INSERT INTO login_attempts (ip, at, success) VALUES (?,?,?)",
        (ip, utc_now(), 1 if success else 0),
    )


# --- 会话 ---------------------------------------------------------------


@dataclass(frozen=True)
class AdminSession:
    session_hash: str
    username: str
    expires_at: str
    csrf_secret: str


def create_session(
    db: Database,
    session_secret: bytes,
    username: str,
    ttl_seconds: int,
    *,
    user_agent: str | None = None,
) -> tuple[str, AdminSession]:
    """创建会话并**轮换**标识（防会话固定）。返回明文 Cookie 值。"""

    token = secrets.token_urlsafe(32)
    shash = session_hash(token, session_secret)
    now = datetime.now(UTC)
    expires = now + timedelta(seconds=ttl_seconds)
    csrf = secrets.token_urlsafe(24)
    with db.write() as conn:
        conn.execute(
            "INSERT INTO admin_sessions (session_hash, created_at, expires_at, last_seen_at, csrf_secret, user_agent) "
            "VALUES (?,?,?,?,?,?)",
            (shash, iso(now), iso(expires), iso(now), csrf, (user_agent or "")[:200]),
        )
    return token, AdminSession(shash, username, iso(expires), csrf)


def load_session(db: Database, session_secret: bytes, token: str) -> AdminSession | None:
    if not token:
        return None
    shash = session_hash(token, session_secret)
    row = db.query_one(
        "SELECT session_hash, expires_at, csrf_secret, revoked_at FROM admin_sessions WHERE session_hash = ?",
        (shash,),
    )
    if row is None or row["revoked_at"]:
        return None
    expires = datetime.fromisoformat(row["expires_at"].replace("Z", "+00:00"))
    if expires <= datetime.now(UTC):
        return None
    db.execute("UPDATE admin_sessions SET last_seen_at = ? WHERE session_hash = ?", (utc_now(), shash))
    return AdminSession(shash, "", row["expires_at"], row["csrf_secret"])


def revoke_session(db: Database, session_secret: bytes, token: str) -> None:
    if not token:
        return
    shash = session_hash(token, session_secret)
    db.execute("UPDATE admin_sessions SET revoked_at = ? WHERE session_hash = ?", (utc_now(), shash))


def purge_expired_sessions(db: Database) -> int:
    with db.write() as conn:
        cur = conn.execute("DELETE FROM admin_sessions WHERE expires_at < ?", (utc_now(),))
        return cur.rowcount or 0


def csrf_token_for(session: AdminSession, session_secret: bytes) -> str:
    """CSRF Token 由会话秘密派生，不额外落库。"""

    return hmac.new(session_secret, ("csrf:" + session.csrf_secret).encode(), hashlib.sha256).hexdigest()


def csrf_valid(session: AdminSession, session_secret: bytes, provided: str | None) -> bool:
    if not provided:
        return False
    return hmac.compare_digest(csrf_token_for(session, session_secret), provided)


# --- 便捷读取 -----------------------------------------------------------


def write_telemetry_key_registry(path: str | Path, mapping: dict[str, str], pepper: Pepper) -> None:
    """Write only HMAC digests. Deliver one-time plaintext via a secure channel."""

    p = Path(path)
    p.parent.mkdir(parents=True, exist_ok=True)
    payload = json.dumps({worker: pepper.digest(token) for worker, token in mapping.items()}, ensure_ascii=False, indent=2) + "\n"
    fd = os.open(p, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    try:
        os.write(fd, payload.encode("utf-8"))
    finally:
        os.close(fd)
