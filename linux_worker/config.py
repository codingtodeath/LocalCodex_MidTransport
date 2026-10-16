from pathlib import Path
from typing import Literal
from pydantic import model_validator
from windows.config import Config, TelemetryConfig


class LocalTelemetry(TelemetryConfig):
    @model_validator(mode="after")
    def fixed_https(self):
        if self.url != "http://127.0.0.1:8083/internal/v1/telemetry":
            raise ValueError("server_telemetry_requires_fixed_loopback")
        return self


class LinuxConfig(Config):
    telemetry: LocalTelemetry
    activation_file: Path
    host_platform: Literal["linux"] = "linux"

    @model_validator(mode="after")
    def managed_home(self):
        if self.enable_mock or self.adapter.mode != "existing_codex_login":
            raise ValueError("server_managed_chatgpt_login_required")
        if self.adapter.existing_auth_file != self.quota.codex_home / "auth.json":
            raise ValueError("single_managed_auth_home_required")
        if self.ssh.host or self.bridge.port != 8765:
            raise ValueError("server_worker_does_not_use_ssh_tunnel")
        if not self.activation_file.is_absolute() or not self.activation_file.resolve().is_relative_to(self.runtime_dir):
            raise ValueError("activation_file_outside_runtime")
        return self
