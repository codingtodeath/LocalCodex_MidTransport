import json
import os
import re
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator


class Strict(BaseModel):
    model_config = ConfigDict(extra="forbid")


class BridgeConfig(Strict):
    host: Literal["127.0.0.1"] = "127.0.0.1"
    port: Literal[8765] = 8765
    auth_secret_file: Path
    max_concurrency: int = Field(default=2, ge=1, le=100)
    max_body_bytes: int = Field(default=33554432, ge=1, le=33554432)
    connect_seconds: float = Field(default=5, gt=0)
    idle_seconds: float = Field(default=300, gt=0)
    total_seconds: float = Field(default=1800, gt=0)


class AdapterConfig(Strict):
    mode: Literal["cliproxyapi", "existing_codex_login"] = "cliproxyapi"
    existing_auth_file: Path | None = None
    outbound_proxy: str | None = None
    catalog_client_version: str = Field(default="0.160.0", pattern=r"^\d+\.\d+\.\d+$", max_length=32)
    cache_diagnostics_capture_inputs: bool = False
    incident_capture_bodies: bool = False
    safe_retry_attempts: int = Field(default=3, ge=1, le=3)
    safe_retry_budget_seconds: float = Field(default=35, gt=0, le=60)
    base_url: Literal["http://127.0.0.1:8317"] = "http://127.0.0.1:8317"
    api_key_file: Path
    config_file: Path
    executable: Path | None = None
    pinned_version: str = "8.0.13"
    sha256: str | None = None

    @model_validator(mode="after")
    def local_proxy(self):
        if self.outbound_proxy:
            from urllib.parse import urlsplit
            u = urlsplit(self.outbound_proxy)
            if u.scheme != "http" or u.hostname != "127.0.0.1" or not u.port or u.username or u.password or u.query or u.fragment or u.path not in ("", "/"):
                raise ValueError("explicit_loopback_http_proxy_required")
        return self


class QuotaConfig(Strict):
    mode: Literal["codex_app_server_stdio", "disabled"] = "codex_app_server_stdio"
    codex_home: Path
    executable: Path | None = None
    pinned_version: str | None = None
    sha256: str | None = None
    poll_seconds: int = Field(default=120, ge=30)
    minimum_refresh_seconds: int = Field(default=30, ge=30)
    request_timeout_seconds: float = Field(default=15, gt=0)
    account_alias: str | None = Field(default=None, max_length=48)


class TelemetryConfig(Strict):
    url: str | None = None
    ca_cert_file: Path | None = None
    secret_file: Path
    interval_seconds: int = Field(default=10, ge=1)
    tls_verify: Literal[True] = True

    @model_validator(mode="after")
    def fixed_https(self):
        from urllib.parse import urlsplit
        if self.url:
            u = urlsplit(self.url)
            if u.scheme != "https" or not u.hostname or u.username or u.password or u.query or u.fragment or u.path != "/internal/v1/telemetry":
                raise ValueError("telemetry requires a fixed HTTPS telemetry endpoint")
        return self


class SSHConfig(Strict):
    host: str | None = None
    port: int = Field(default=22, ge=1, le=65535)
    user: str = "codex-tunnel"
    executable: Path = Path("C:/Windows/System32/OpenSSH/ssh.exe")
    identity_file: Path
    known_hosts_file: Path
    remote_bind: Literal["127.0.0.1:18765"] = "127.0.0.1:18765"
    local_target: Literal["127.0.0.1:8765"] = "127.0.0.1:8765"
    strict_host_key_checking: Literal[True] = True

    @model_validator(mode="after")
    def safe_destination(self):
        if self.host and not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9.:-]{0,252}", self.host):
            raise ValueError("invalid SSH hostname")
        if not re.fullmatch(r"[a-zA-Z0-9_][a-zA-Z0-9_-]{0,47}", self.user):
            raise ValueError("invalid SSH username")
        return self


class Config(Strict):
    environment: Literal["development", "production"] = "development"
    protocol_version: Literal["1.0"] = "1.0"
    worker_id: str = Field(default="home-windows", pattern=r"^[a-zA-Z0-9_-]{1,48}$")
    runtime_dir: Path
    bridge: BridgeConfig
    adapter: AdapterConfig
    quota: QuotaConfig
    telemetry: TelemetryConfig
    ssh: SSHConfig
    models_allowlist: list[str] = Field(default_factory=list, max_length=64)
    inference_retries: Literal[0] = 0
    log_bodies: Literal[False] = False
    enable_mock: bool = False

    @model_validator(mode="after")
    def production(self):
        if self.adapter.mode == "existing_codex_login":
            auth = self.adapter.existing_auth_file
            if not auth or not auth.is_absolute() or auth.name != "auth.json" or "onedrive" in str(auth).lower():
                raise ValueError("explicit_existing_codex_auth_file_required")
            if self.enable_mock:
                raise ValueError("mock_cannot_read_existing_auth")
        paths = (self.runtime_dir, self.bridge.auth_secret_file, self.adapter.api_key_file,
                 self.adapter.config_file, self.quota.codex_home, self.telemetry.secret_file,
                 self.ssh.identity_file, self.ssh.known_hosts_file)
        if any(not p.is_absolute() for p in paths):
            raise ValueError("runtime and secret paths must be absolute")
        root = self.runtime_dir.parent.resolve()
        if "onedrive" in str(root).lower() or any(not p.resolve().is_relative_to(root) for p in paths):
            raise ValueError("all runtime and secret paths must share an isolated unsynced root")
        if self.enable_mock and (self.environment == "production" or self.telemetry.url or self.ssh.host):
            raise ValueError("mock must stay local and cannot report to a server")
        if self.environment == "production" and not self.models_allowlist:
            raise ValueError("production requires verified model IDs")
        return self


def load_config(path: str | Path) -> Config:
    def expand(obj):
        if isinstance(obj, str):
            return os.path.expandvars(obj)
        if isinstance(obj, list):
            return [expand(x) for x in obj]
        if isinstance(obj, dict):
            return {k: expand(v) for k, v in obj.items()}
        return obj
    return Config.model_validate(expand(json.loads(Path(path).read_text(encoding="utf-8-sig"))))


def read_key(path: Path, prefix: str) -> str:
    key = path.read_text(encoding="utf-8-sig").strip()
    if not re.fullmatch(re.escape(prefix) + r"[A-Za-z0-9_-]{43,}", key):
        raise ValueError("invalid service key")
    return key
