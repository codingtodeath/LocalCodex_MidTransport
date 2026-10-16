"""配置加载与严格校验。

设计要点（见 01_DEVELOPMENT_SPEC.md 第 5 节、examples/README.md）：
* 路径中的变量显式展开；变量不存在就报错，不静默使用字符串路径。
* 生产环境拒绝 `YOUR_...` / `REPLACE_...` 占位符。
* 生产环境拒绝 `enable_mock=true`。
* 生产环境不加载 `prices.fixture.json`（其 `test_only=true`）。
* 秘密不写进配置文件：只保存秘密文件的路径，运行时读取。
"""

from __future__ import annotations

import json
import os
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

PLACEHOLDER_RE = re.compile(r"(YOUR_|REPLACE_|\$\{|\{\{)")

# 生产环境绝不允许为真的开关。
_PROD_FORBIDDEN_TRUE = ("enable_mock",)


class ConfigError(Exception):
    """配置非法。启动即失败，不带着错误配置运行。"""


def _expand(value: str) -> str:
    """展开 ${VAR} / $VAR / %VAR%；变量不存在时报错。"""

    pattern = re.compile(r"\$\{([A-Za-z_][A-Za-z0-9_]*)\}|%([A-Za-z_][A-Za-z0-9_]*)%")

    def repl(match: re.Match[str]) -> str:
        name = match.group(1) or match.group(2)
        if name not in os.environ:
            raise ConfigError(
                f"配置引用了未定义的环境变量 {name!r}；不静默当作字面路径处理。"
            )
        return os.environ[name]

    return pattern.sub(repl, value)


def _expand_path(value: str) -> str:
    return _expand(value)


@dataclass(frozen=True)
class RelayConfig:
    """服务端运行配置。字段与 examples/server.config.example.json 对齐。"""

    environment: str = "development"
    protocol_version: str = "1.0"

    listen_host: str = "127.0.0.1"
    listen_port: int = 8080

    db_path: str = "var/relay.db"

    worker_id: str = "home-windows"
    worker_base_url: str = "http://127.0.0.1:18765"

    worker_auth_secret_file: str = "var/secrets/wkr.key"
    telemetry_key_registry: str = "var/secrets/telemetry-key-hashes.json"
    api_key_pepper_file: str = "var/secrets/key-pepper"
    session_secret_file: str = "var/secrets/session.key"

    max_concurrency: int = 2
    per_client_concurrency: int = 1

    heartbeat_stale_seconds: int = 35
    probe_interval_seconds: int = 15
    probe_timeout_seconds: int = 2
    quota_stale_seconds: int = 300

    max_body_bytes: int = 32 * 1024 * 1024
    max_telemetry_bytes: int = 64 * 1024

    connect_timeout_seconds: int = 5
    stream_idle_timeout_seconds: int = 300
    request_deadline_seconds: int = 1800

    inference_retries: int = 0
    log_bodies: bool = False
    enable_mock: bool = False

    models_allowlist: tuple[str, ...] = ()
    trusted_proxy_hosts: tuple[str, ...] = ("127.0.0.1",)

    command_ttl_seconds: int = 60
    command_min_refresh_interval_seconds: int = 30

    admin_session_seconds: int = 12 * 3600
    login_rate_limit_per_minute: int = 10

    retention_request_days: int = 90
    retention_rollup_days: int = 365
    retention_quota_days: int = 30
    retention_command_days: int = 30

    public_base_url: str = ""
    price_rules: tuple[dict[str, Any], ...] = field(default_factory=tuple)

    # 派生
    @property
    def is_production(self) -> bool:
        return self.environment.lower() in ("production", "prod")

    @property
    def db_file(self) -> Path:
        return Path(self.db_path)


def _require_type(raw: dict[str, Any], key: str, types: tuple[type, ...]) -> Any:
    if key not in raw:
        return None
    value = raw[key]
    # bool 是 int 的子类，单独排除以免 True 被当成端口。
    if isinstance(value, bool) and bool not in types:
        raise ConfigError(f"配置项 {key} 类型错误：期望 {types}，得到 bool")
    if not isinstance(value, types):
        raise ConfigError(f"配置项 {key} 类型错误：期望 {types}，得到 {type(value).__name__}")
    return value


def load_config(path: str | os.PathLike[str] | None = None) -> RelayConfig:
    """从 JSON 文件加载配置。path 为 None 时使用全默认（仅测试/开发）。"""

    raw: dict[str, Any] = {}
    if path is not None:
        config_path = Path(path)
        if not config_path.is_file():
            raise ConfigError(f"配置文件不存在：{config_path}")
        text = config_path.read_text(encoding="utf-8")
        try:
            raw = json.loads(text)
        except json.JSONDecodeError as exc:
            raise ConfigError(f"配置文件不是合法 JSON：{exc}") from exc
        if not isinstance(raw, dict):
            raise ConfigError("配置文件顶层必须是 JSON 对象")

    known = {f.name for f in RelayConfig.__dataclass_fields__.values()}  # type: ignore[attr-defined]
    unknown = sorted(set(raw) - known)
    if unknown:
        # 严格校验：拼错的配置项不能静默忽略。
        raise ConfigError(f"配置包含未知项：{', '.join(unknown)}")

    kwargs: dict[str, Any] = {}
    for name in known:
        if name not in raw:
            continue
        value = raw[name]
        if name in (
            "listen_port",
            "max_concurrency",
            "per_client_concurrency",
            "heartbeat_stale_seconds",
            "probe_interval_seconds",
            "probe_timeout_seconds",
            "quota_stale_seconds",
            "max_body_bytes",
            "max_telemetry_bytes",
            "connect_timeout_seconds",
            "stream_idle_timeout_seconds",
            "request_deadline_seconds",
            "inference_retries",
            "command_ttl_seconds",
            "command_min_refresh_interval_seconds",
            "admin_session_seconds",
            "login_rate_limit_per_minute",
            "retention_request_days",
            "retention_rollup_days",
            "retention_quota_days",
            "retention_command_days",
        ):
            if isinstance(value, bool) or not isinstance(value, int):
                raise ConfigError(f"配置项 {name} 必须是整数")
            kwargs[name] = value
        elif name in ("log_bodies", "enable_mock"):
            if not isinstance(value, bool):
                raise ConfigError(f"配置项 {name} 必须是布尔值")
            kwargs[name] = value
        elif name in ("models_allowlist", "trusted_proxy_hosts"):
            if not isinstance(value, list) or not all(isinstance(v, str) for v in value):
                raise ConfigError(f"配置项 {name} 必须是字符串数组")
            kwargs[name] = tuple(value)
        elif name == "price_rules":
            if not isinstance(value, list):
                raise ConfigError("配置项 price_rules 必须是数组")
            kwargs[name] = tuple(value)
        elif name.endswith("_file") or name in ("db_path",):
            if not isinstance(value, str):
                raise ConfigError(f"配置项 {name} 必须是字符串")
            kwargs[name] = _expand_path(value)
        elif name in ("environment", "protocol_version", "listen_host", "worker_id",
                      "worker_base_url", "public_base_url"):
            if not isinstance(value, str):
                raise ConfigError(f"配置项 {name} 必须是字符串")
            kwargs[name] = _expand(value) if name == "public_base_url" else value
        else:
            kwargs[name] = value

    config = RelayConfig(**kwargs)

    # 端口与并发边界
    if not (1 <= config.listen_port <= 65535):
        raise ConfigError("listen_port 必须在 1..65535")
    if config.max_concurrency < 1:
        raise ConfigError("max_concurrency 必须 >= 1")
    if config.per_client_concurrency < 1:
        raise ConfigError("per_client_concurrency 必须 >= 1")
    if config.per_client_concurrency > config.max_concurrency:
        raise ConfigError("per_client_concurrency 不能大于 max_concurrency")
    if config.max_body_bytes < 1024:
        raise ConfigError("max_body_bytes 过小")
    if config.max_telemetry_bytes < 1024 or config.max_telemetry_bytes > 1024 * 1024:
        raise ConfigError("max_telemetry_bytes 超出合理范围")
    if config.protocol_version != "1.0":
        raise ConfigError("本版本只支持 protocol_version=1.0")
    if config.listen_host != "127.0.0.1" or config.worker_base_url not in ("http://127.0.0.1:18765", "http://127.0.0.1:8765"):
        raise ConfigError("Gateway and Bridge must use their fixed loopback endpoints")
    if config.log_bodies or config.inference_retries != 0:
        raise ConfigError("Body logging and inference retries must be disabled")

    if config.is_production:
        _validate_production(config)

    return config


def _validate_production(config: RelayConfig) -> None:
    """生产环境防误用检查：占位符、Mock、测试价格、宽松设置一律拒绝。"""

    problems: list[str] = []

    for name in _PROD_FORBIDDEN_TRUE:
        if getattr(config, name):
            problems.append(f"生产环境不允许 {name}=true")

    if not config.models_allowlist:
        problems.append("生产环境必须显式配置 models_allowlist，不能为空")

    if not config.public_base_url:
        problems.append("生产环境必须配置 public_base_url")
    elif PLACEHOLDER_RE.search(config.public_base_url):
        problems.append("public_base_url 仍含占位符，未按实际域名替换")

    for name in (
        "db_path",
        "worker_auth_secret_file",
        "telemetry_key_registry",
        "api_key_pepper_file",
        "session_secret_file",
    ):
        value = getattr(config, name)
        if PLACEHOLDER_RE.search(str(value)):
            problems.append(f"{name} 仍含占位符")

    for rule in config.price_rules:
        if not isinstance(rule, dict):
            problems.append("price_rules 项必须是对象")
            continue
        if rule.get("test_only"):
            problems.append("生产环境拒绝加载 test_only=true 的价格规则")

    if config.inference_retries != 0:
        problems.append("生产环境必须关闭自动推理重试（inference_retries=0）")

    if config.log_bodies:
        problems.append("生产环境必须关闭正文日志（log_bodies=false）")

    if "*" in config.trusted_proxy_hosts:
        problems.append("trusted_proxy_hosts 不允许通配 *")

    if problems:
        raise ConfigError("生产配置检查失败：\n  - " + "\n  - ".join(problems))


def load_price_rules_file(path: str | os.PathLike[str], *, allow_test_only: bool) -> list[dict[str, Any]]:
    """读取价格规则文件。生产拒绝 test_only 规则。"""

    p = Path(path)
    if not p.is_file():
        raise ConfigError(f"价格规则文件不存在：{p}")
    data = json.loads(p.read_text(encoding="utf-8"))
    if not isinstance(data, dict):
        raise ConfigError("价格规则文件顶层必须是对象")
    if data.get("test_only") and not allow_test_only:
        raise ConfigError("拒绝在生产加载 test_only=true 的价格规则文件")
    rules = data.get("rules")
    if not isinstance(rules, list):
        raise ConfigError("价格规则文件缺少 rules 数组")
    return rules
