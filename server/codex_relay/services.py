"""依赖容器。

把配置、数据库、秘密、价格簿与并发闸门集中在一处，供路由通过
`request.app.state.services` 取用。测试可直接注入内存数据库与假 Bridge。
"""

from __future__ import annotations

import threading
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from . import pricing as pricing_mod
from .auth import Pepper
from .config import RelayConfig
from .db import Database


class ConcurrencyGate:
    """全局 + 单客户端并发闸门。

    见 01_DEVELOPMENT_SPEC.md 第 16 节：初始上限 Windows 单机 2、服务器总 2、
    单客户端 1；满额直接 429，不建立无限等待队列。

    服务器侧的闸门只是第一层保护；Windows 本地限制才是最终保护。
    """

    def __init__(self, global_limit: int, per_client_limit: int) -> None:
        self.global_limit = global_limit
        self.per_client_limit = per_client_limit
        self._lock = threading.Lock()
        self._global_active = 0
        self._per_client: dict[str, int] = {}

    @property
    def global_active(self) -> int:
        with self._lock:
            return self._global_active

    def active_for(self, client_key_id: str) -> int:
        with self._lock:
            return self._per_client.get(client_key_id, 0)

    def snapshot(self) -> dict[str, int]:
        with self._lock:
            return {"global": self._global_active, "per_client": dict(self._per_client)}

    def try_acquire(self, client_key_id: str) -> str | None:
        """成功返回 None；失败返回拒绝原因。"""

        with self._lock:
            if self._global_active >= self.global_limit:
                return "gateway_concurrency_limit"
            if self._per_client.get(client_key_id, 0) >= self.per_client_limit:
                return "gateway_concurrency_limit"
            self._global_active += 1
            self._per_client[client_key_id] = self._per_client.get(client_key_id, 0) + 1
            return None

    def release(self, client_key_id: str) -> None:
        with self._lock:
            if self._global_active > 0:
                self._global_active -= 1
            current = self._per_client.get(client_key_id, 0)
            if current <= 1:
                self._per_client.pop(client_key_id, None)
            else:
                self._per_client[client_key_id] = current - 1


@dataclass
class Services:
    config: RelayConfig
    db: Database
    pepper: Pepper
    session_secret: bytes
    worker_key: str | None = None
    price_book: pricing_mod.PriceBook = field(default_factory=pricing_mod.PriceBook)
    gate: ConcurrencyGate | None = None
    _http_client: Any = None

    def __post_init__(self) -> None:
        from .connection_log import ConnectionLog
        self.connection_log = ConnectionLog(self.db.path.parent / "logs/gateway-events.jsonl")
        if self.gate is None:
            self.gate = ConcurrencyGate(
                self.config.max_concurrency, self.config.per_client_concurrency
            )

    def reload_prices(self) -> int:
        rules = pricing_mod.load_rules_for_db(self.db)
        self.price_book = pricing_mod.PriceBook(rules)
        return len(rules)


# --- 便捷构造（同时供 CLI 与测试使用） ----------------------------------


def build_services(
    config: RelayConfig,
    *,
    pepper_bytes: bytes,
    session_secret: bytes,
    worker_key: str | None = None,
    create_missing_secrets: bool = False,
) -> Services:
    from .auth import read_secret_file

    db = Database(config.db_path)
    db.migrate()

    if pepper_bytes is None:
        if not create_missing_secrets:
            raise ValueError("必须提供 pepper")
        pepper_bytes = read_secret_file(config.api_key_pepper_file, create=True)
    if session_secret is None:
        if not create_missing_secrets:
            raise ValueError("必须提供 session_secret")
        session_secret = read_secret_file(config.session_secret_file, create=True)

    services = Services(
        config=config,
        db=db,
        pepper=Pepper(pepper_bytes),
        session_secret=session_secret,
        worker_key=worker_key,
    )
    services.reload_prices()
    return services


def secret_paths_exist(config: RelayConfig) -> dict[str, bool]:
    return {
        name: Path(getattr(config, name)).is_file()
        for name in (
            "worker_auth_secret_file",
            "api_key_pepper_file",
            "session_secret_file",
        )
    }
