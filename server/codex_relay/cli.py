"""命令行入口：init / serve / doctor / key 管理。

用法：
    python -m codex_relay.cli init --config CONFIG.json [--admin-user admin]
    python -m codex_relay.cli serve --config CONFIG.json
    python -m codex_relay.cli doctor --config CONFIG.json
    python -m codex_relay.cli create-client-key --config CONFIG.json --alias phone
    python -m codex_relay.cli revoke-client-key --config CONFIG.json --key-id ck_xxx
    python -m codex_relay.cli set-telemetry-key --config CONFIG.json --worker home-windows
    python -m codex_relay.cli set-worker-key --config CONFIG.json
    python -m codex_relay.cli status --config CONFIG.json
    python -m codex_relay.cli backup --config CONFIG.json --out backup.db
"""

from __future__ import annotations

import argparse
import getpass
import json
import os
import sys
from pathlib import Path

from . import __version__
from . import auth as auth_mod
from . import quota_store, worker_state
from .config import ConfigError, RelayConfig, load_config
from .db import Database, cleanup, utc_now
from .services import Services, build_services


def _load(args: argparse.Namespace) -> RelayConfig:
    return load_config(args.config) if args.config else RelayConfig()


def _services(config: RelayConfig, *, create: bool) -> Services:
    from .auth import read_secret_file

    pepper = read_secret_file(config.api_key_pepper_file, create=create) if _exists(config.api_key_pepper_file) or create else None
    session = read_secret_file(config.session_secret_file, create=create) if _exists(config.session_secret_file) or create else None
    worker_key = None
    if _exists(config.worker_auth_secret_file):
        worker_key = read_secret_file(config.worker_auth_secret_file).decode("utf-8").strip()
    services = build_services(
        config,
        pepper_bytes=pepper,  # type: ignore[arg-type]
        session_secret=session,  # type: ignore[arg-type]
        worker_key=worker_key,
        create_missing_secrets=create,
    )
    return services


def _exists(path: str) -> bool:
    return Path(path).is_file()


# --- 子命令 -------------------------------------------------------------


def cmd_init(args: argparse.Namespace) -> int:
    config = _load(args)
    print(f"环境：{config.environment}")
    print(f"数据库：{config.db_path}")
    print(f"监听：{config.listen_host}:{config.listen_port}")

    services = _services(config, create=True)
    print(f"迁移完成，schema 版本 {_schema_version(services.db)}")

    # wkr Key（Gateway -> Bridge）
    if not _exists(config.worker_auth_secret_file):
        p = Path(config.worker_auth_secret_file)
        p.parent.mkdir(parents=True, exist_ok=True)
        fd = os.open(p, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        try:
            os.write(fd, auth_mod.generate_key(auth_mod.ROLE_WORKER).encode("utf-8"))
        finally:
            os.close(fd)
        print(f"已生成 wkr Key：{p}（0600）")
    else:
        print(f"wkr Key 已存在：{config.worker_auth_secret_file}")

    worker_id = config.worker_id
    if auth_mod.verify_worker_credential(services.db, auth_mod.ROLE_WORKER, "", services.pepper) is None:
        pass

    # 遥测 Key（Windows -> Ubuntu）
    existing = services.db.query_one(
        "SELECT worker_id FROM worker_credentials WHERE worker_id = ? AND role = ? AND revoked_at IS NULL",
        (worker_id, auth_mod.ROLE_TELEMETRY),
    )
    tel_token = None
    if existing is None:
        tel_token = auth_mod.generate_key(auth_mod.ROLE_TELEMETRY)
        auth_mod.upsert_worker_credential(services.db, worker_id, auth_mod.ROLE_TELEMETRY, tel_token, services.pepper)
        print(f"已生成 {worker_id} 的 tel Key（明文只显示这一次）：")
        print(f"  {tel_token}")
    else:
        print(f"{worker_id} 的 tel Key 已存在（服务器只存摘要，无法显示明文）。")

    worker_state.ensure_worker(services.db, worker_id, display_name=args.worker_display or worker_id)

    # 管理员
    admin_user = args.admin_user
    if admin_user and services.db.query_one("SELECT 1 FROM admin_credentials WHERE username = ?", (admin_user,)) is None:
        password = args.admin_password
        if not password:
            if sys.stdin.isatty():
                password = getpass.getpass("设置管理员密码（不回显）：")
                confirm = getpass.getpass("再次输入：")
                if password != confirm:
                    print("两次输入不一致。", file=sys.stderr)
                    return 1
            else:
                print("非交互环境必须通过 --admin-password-stdin 提供密码。", file=sys.stderr)
                return 1
        try:
            auth_mod.create_admin(services.db, admin_user, password)
        except ValueError as exc:
            print(f"管理员创建失败：{exc}", file=sys.stderr)
            return 1
        print(f"已创建管理员：{admin_user}")
    elif admin_user:
        print(f"管理员已存在：{admin_user}")

    return 0


def _schema_version(db: Database) -> int:
    row = db.query_one("SELECT MAX(version) AS v FROM schema_migrations")
    return int(row["v"]) if row and row["v"] is not None else 0


def cmd_serve(args: argparse.Namespace) -> int:
    import uvicorn

    config = _load(args)
    services = _services(config, create=False)
    from .app import create_app

    app = create_app(config, services=services)
    uvicorn.run(
        app,
        host=config.listen_host,
        port=config.listen_port,
        workers=1,
        log_level=args.log_level,
        access_log=False,  # 不开 access log，避免记录请求细节
        proxy_headers=False,
        server_header=False,
        date_header=False,
    )
    return 0


def cmd_doctor(args: argparse.Namespace) -> int:
    config = _load(args)
    problems: list[str] = []
    notes: list[str] = []

    print(f"codex-relay {__version__}  doctor")
    print(f"环境：{config.environment}")
    print(f"协议版本：{config.protocol_version}")
    print(f"监听：{config.listen_host}:{config.listen_port}"
          f"{'（仅 loopback，未对外暴露）' if config.listen_host == '127.0.0.1' else '（警告：非 loopback）'}")
    print(f"Bridge 目标：{config.worker_base_url}")
    print(f"并发上限：全局 {config.max_concurrency} / 单客户端 {config.per_client_concurrency}")
    print(f"请求体上限：{config.max_body_bytes} 字节；遥测上限：{config.max_telemetry_bytes} 字节")
    print(f"推理自动重试：{config.inference_retries}")
    print(f"正文日志：{config.log_bodies}")

    # 秘密文件检查复用 auth.read_secret_file 的策略：它同时校验存在性、权限
    # （允许 0640，拒绝其他用户可读/属组可写）与长度。doctor 不再自己写一套
    # 判定，避免实现与自检出现分歧。
    from .auth import SecretStoreError, read_secret_file

    for name in ("worker_auth_secret_file", "api_key_pepper_file", "session_secret_file"):
        path = Path(getattr(config, name))
        if not path.is_file():
            problems.append(f"缺少秘密文件：{path}")
            print(f"  [缺失] {name}: {path}")
            continue
        mode = path.stat().st_mode & 0o777
        try:
            read_secret_file(path)
        except SecretStoreError as exc:
            problems.append(f"{name} 不合法：{exc}")
            print(f"  [不合法] {name}: {path} ({oct(mode)})")
        else:
            print(f"  [OK] {name}: {path} ({oct(mode)})")

    if config.listen_host != "127.0.0.1":
        problems.append("listen_host 不是 127.0.0.1；本服务不应直接暴露公网。")
    if not config.worker_base_url.startswith("http://127.0.0.1:"):
        problems.append("worker_base_url 必须是本机反向隧道地址（127.0.0.1）。")
    if config.enable_mock and config.is_production:
        problems.append("生产环境不允许 enable_mock。")
    if config.log_bodies:
        problems.append("log_bodies=true 会导致正文落盘。")

    if _exists(config.db_path):
        db = Database(config.db_path)
        try:
            print(f"数据库：{config.db_path}（schema {_schema_version(db)}，完整检查 {'通过' if db.integrity_ok() else '失败'}）")
            if not db.integrity_ok():
                problems.append("数据库完整性检查失败。")
            row = db.query_one("SELECT COUNT(*) AS n FROM client_keys WHERE revoked_at IS NULL")
            print(f"有效客户端 Key：{int(row['n']) if row else 0}")
            row = db.query_one("SELECT COUNT(*) AS n FROM price_rules")
            print(f"价格规则：{int(row['n']) if row else 0}")
            print(f"额度快照：{'有' if quota_store.get_current_snapshot(db, config.worker_id) else '无'}")
        finally:
            db.close()
    else:
        notes.append(f"数据库尚不存在：{config.db_path}")

    print("\n结论：" + ("通过，未发现阻断问题。" if not problems else "存在问题："))
    for problem in problems:
        print(f"  - {problem}")
    for note in notes:
        print(f"  (提示) {note}")
    print("\n本 doctor 不上传任何数据，也不读取任何 Codex 订阅凭证。")
    return 1 if problems else 0


def cmd_create_client_key(args: argparse.Namespace) -> int:
    config = _load(args)
    services = _services(config, create=False)
    token, key = auth_mod.create_client_key(services.db, services.pepper, args.alias)
    print(f"key_id: {key.key_id}")
    print(f"alias : {key.alias}")
    print("明文 Key（只显示这一次，服务器只保存摘要）：")
    print(token)
    return 0


def cmd_revoke_client_key(args: argparse.Namespace) -> int:
    config = _load(args)
    services = _services(config, create=False)
    if auth_mod.revoke_client_key(services.db, args.key_id):
        print(f"已撤销 {args.key_id}")
        return 0
    print("未找到或已撤销。", file=sys.stderr)
    return 1


def cmd_set_telemetry_key(args: argparse.Namespace) -> int:
    config = _load(args)
    services = _services(config, create=False)
    token = auth_mod.generate_key(auth_mod.ROLE_TELEMETRY)
    auth_mod.upsert_worker_credential(services.db, args.worker, auth_mod.ROLE_TELEMETRY, token, services.pepper)
    print(f"worker_id: {args.worker}")
    print("新的 tel Key（只显示这一次）：")
    print(token)
    return 0


def cmd_set_worker_key(args: argparse.Namespace) -> int:
    config = _load(args)
    services = _services(config, create=False)
    token = auth_mod.generate_key(auth_mod.ROLE_WORKER)
    p = Path(config.worker_auth_secret_file)
    p.parent.mkdir(parents=True, exist_ok=True)
    fd = os.open(p, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    try:
        os.write(fd, token.encode("utf-8"))
    finally:
        os.close(fd)
    print(f"已写入 {p}（0600）。明文：")
    print(token)
    print("请同时更新 Windows Bridge 的对应秘密。")
    return 0


def cmd_create_admin(args: argparse.Namespace) -> int:
    config = _load(args)
    services = _services(config, create=False)
    password = args.password
    if not password:
        if sys.stdin.isatty():
            password = getpass.getpass("管理员密码（不回显）：")
            confirm = getpass.getpass("再次输入：")
            if password != confirm:
                print("两次输入不一致。", file=sys.stderr)
                return 1
        else:
            print("非交互环境必须提供 --password-stdin。", file=sys.stderr)
            return 1
    try:
        auth_mod.create_admin(services.db, args.username, password)
    except ValueError as exc:
        print(f"失败：{exc}", file=sys.stderr)
        return 1
    print(f"已设置管理员 {args.username}")
    return 0


def cmd_status(args: argparse.Namespace) -> int:
    config = _load(args)
    services = _services(config, create=False)
    snapshot = worker_state.evaluate(services.db, config.worker_id, config)
    payload = {
        "worker": snapshot.as_dict(),
        "quota": quota_store.build_view(
            services.db,
            config.worker_id,
            config,
            worker_online=snapshot.state == worker_state.STATE_ONLINE,
        ).as_dict(),
        "gate": services.gate.snapshot() if services.gate else {},
        "server_time": utc_now(),
    }
    print(json.dumps(payload, ensure_ascii=False, indent=2))
    return 0


def cmd_backup(args: argparse.Namespace) -> int:
    config = _load(args)
    db = Database(config.db_path)
    try:
        target = db.backup_to(args.out)
    finally:
        db.close()
    print(f"已备份到 {target}")
    print("备份只包含本项目数据；不含任何 Codex OAuth 文件。")
    return 0


def cmd_cleanup(args: argparse.Namespace) -> int:
    config = _load(args)
    db = Database(config.db_path)
    try:
        removed = cleanup(db, config)
    finally:
        db.close()
    print(f"清理完成：{json.dumps(removed, ensure_ascii=False)}")
    return 0


# --- 参数解析 -----------------------------------------------------------


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="codex_relay", description="Codex 私有中继服务端")
    parser.add_argument("--version", action="version", version=__version__)
    sub = parser.add_subparsers(dest="command", required=True)

    def add_config(p: argparse.ArgumentParser) -> None:
        p.add_argument("--config", help="server.config.json 路径（省略则用内置开发默认值）")

    p = sub.add_parser("init", help="创建秘密、迁移数据库、创建管理员与遥测 Key")
    add_config(p)
    p.add_argument("--admin-user", default="admin")
    p.add_argument("--admin-password", help="仅建议在受控测试中使用")
    p.add_argument("--worker-display", default=None)
    p.set_defaults(func=cmd_init)

    p = sub.add_parser("serve", help="启动 Gateway（单 worker Uvicorn）")
    add_config(p)
    p.add_argument("--log-level", default="info")
    p.set_defaults(func=cmd_serve)

    p = sub.add_parser("doctor", help="只读自检：版本、端口、权限、数据库")
    add_config(p)
    p.set_defaults(func=cmd_doctor)

    p = sub.add_parser("create-client-key", help="创建客户端网关 Key")
    add_config(p)
    p.add_argument("--alias", required=True)
    p.set_defaults(func=cmd_create_client_key)

    p = sub.add_parser("revoke-client-key", help="撤销客户端网关 Key")
    add_config(p)
    p.add_argument("--key-id", required=True)
    p.set_defaults(func=cmd_revoke_client_key)

    p = sub.add_parser("set-telemetry-key", help="为 worker 重新生成 tel Key")
    add_config(p)
    p.add_argument("--worker", required=True)
    p.set_defaults(func=cmd_set_telemetry_key)

    p = sub.add_parser("set-worker-key", help="重新生成 wkr Key（Gateway -> Bridge）")
    add_config(p)
    p.set_defaults(func=cmd_set_worker_key)

    p = sub.add_parser("create-admin", help="创建或重置管理员密码")
    add_config(p)
    p.add_argument("--username", required=True)
    p.add_argument("--password", help="仅建议在受控测试中使用")
    p.set_defaults(func=cmd_create_admin)

    p = sub.add_parser("status", help="打印当前 worker / 额度 / 并发状态")
    add_config(p)
    p.set_defaults(func=cmd_status)

    p = sub.add_parser("backup", help="SQLite 在线备份")
    add_config(p)
    p.add_argument("--out", required=True)
    p.set_defaults(func=cmd_backup)

    p = sub.add_parser("cleanup", help="按保留期清理")
    add_config(p)
    p.set_defaults(func=cmd_cleanup)

    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        return int(args.func(args))
    except (ConfigError, auth_mod.SecretStoreError) as exc:
        print(f"配置/秘密错误：{exc}", file=sys.stderr)
        return 2
    except FileNotFoundError as exc:
        print(f"文件缺失：{exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
