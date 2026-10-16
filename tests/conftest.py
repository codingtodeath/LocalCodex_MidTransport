import json
import secrets
import pytest
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "server"))

from windows.config import Config
from windows.state import State


@pytest.fixture
def config(tmp_path):
    for name in ("runtime", "secrets", "quota-codex", "proxy-auth"):
        (tmp_path / name).mkdir()
    for prefix in ("wkr", "cpa", "tel"):
        (tmp_path / "secrets" / f"{prefix}.key").write_text(prefix + "_" + secrets.token_urlsafe(32))
    return Config.model_validate(dict(runtime_dir=str(tmp_path / "runtime"),
        bridge=dict(auth_secret_file=str(tmp_path / "secrets/wkr.key")),
        adapter=dict(api_key_file=str(tmp_path / "secrets/cpa.key"), config_file=str(tmp_path / "proxy-config.yaml")),
        quota=dict(codex_home=str(tmp_path / "quota-codex")),
        telemetry=dict(secret_file=str(tmp_path / "secrets/tel.key")),
        ssh=dict(identity_file=str(tmp_path / "ssh.key"), known_hosts_file=str(tmp_path / "known_hosts")),
        models_allowlist=["mock-codex"], enable_mock=True))


@pytest.fixture
def state():
    return State("home-windows")
