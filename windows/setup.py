"""Generate keys directly to a protected local directory; never print them."""
import hashlib
import json
import os
import secrets
import subprocess
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parents[1]
from .security import secure_dir


def proxy_document(root, key):
    return {"config-version": 8,
        "server": {"host": "127.0.0.1", "port": 8317, "trusted-proxies": [],
                   "commercial-mode": True, "discovery": {"enabled": False}},
        "management": {"allow-remote": False, "secret-key": "", "disable-control-panel": True,
                       "disable-auto-update-panel": True},
        "access": {"api-keys": [key]},
        "oauth": {"auth-dir": str(root / "proxy-auth")},
        "routing": {"strategy": "fill-first", "retry": {"request-retry": 0, "max-retry-credentials": 1, "max-retry-interval": 0}},
        "quota-exceeded": {"switch-project": False, "switch-preview-model": False},
        "multimedia": {"disable-image-generation": True},
        "observability": {"logs": {"debug": False, "logging-to-file": False, "request-log": False},
                          "usage": {"usage-statistics-enabled": False}, "pprof": {"enable": False}},
        "requests": {"streaming": {"keepalive-seconds": 0, "bootstrap-retries": 0},
                     "passthrough-headers": False, "nonstream-keepalive-interval": 0},
        "plugins": {"enabled": False}}


def discover_codex():
    # Resolve the native npm executable without invoking .cmd through a shell.
    import shutil
    wrapper = shutil.which("codex.cmd")
    if wrapper:
        base = Path(wrapper).parent / "node_modules" / "@openai" / "codex"
        found = list(base.glob("node_modules/@openai/codex-win32-x64/vendor/*/bin/codex.exe"))
        if len(found) == 1:
            return found[0].resolve()
    return None


def setup(mock=False):
    if os.name != "nt":
        raise RuntimeError("setup_requires_windows")
    base = Path(os.environ["LOCALAPPDATA"]) / ("CodexRelay-Dev" if mock else "CodexRelay")
    if "onedrive" in str(base.resolve()).lower():
        raise ValueError("synced_secret_directory_rejected")
    secure_dir(base)
    for name in ("secrets", "proxy-auth", "quota-codex", "runtime", "logs", "ssh"):
        secure_dir(base / name)
    for prefix in ("wkr", "cpa", "tel"):
        target = base / "secrets" / f"{prefix}.key"
        if not target.exists():
            target.write_text(prefix + "_" + secrets.token_urlsafe(32), encoding="utf-8")
    from .config import read_key
    proxy = base / "proxy-config.yaml"
    if not proxy.exists():
        proxy.write_text(yaml.safe_dump(proxy_document(base, read_key(base / "secrets/cpa.key", "cpa_")), allow_unicode=True, sort_keys=False), encoding="utf-8")
    quota_config = base / "quota-codex/config.toml"
    if not quota_config.exists():
        quota_config.write_text('model_provider = "openai"\ncli_auth_credentials_store = "file"\n', encoding="utf-8")
    executable = ROOT / "vendor/cliproxyapi/8.0.13/cli-proxy-api.exe"
    codex = discover_codex()
    config = {"environment": "development", "protocol_version": "1.0", "worker_id": "home-windows",
        "runtime_dir": str(base / "runtime"),
        "bridge": {"auth_secret_file": str(base / "secrets/wkr.key")},
        "adapter": {"api_key_file": str(base / "secrets/cpa.key"), "config_file": str(proxy),
                    "executable": str(executable), "pinned_version": "8.0.13",
                    "sha256": hashlib.sha256(executable.read_bytes()).hexdigest() if executable.exists() else None},
        "quota": {"codex_home": str(base / "quota-codex"), "executable": str(codex) if codex else None,
                  "pinned_version": "0.144.1" if codex else None,
                  "sha256": hashlib.sha256(codex.read_bytes()).hexdigest() if codex else None},
        "telemetry": {"url": None, "secret_file": str(base / "secrets/tel.key")},
        "ssh": {"host": None, "identity_file": str(base / "ssh/relay_ed25519"), "known_hosts_file": str(base / "ssh/known_hosts")},
        "models_allowlist": ["mock-codex"] if mock else [], "enable_mock": mock,
        "inference_retries": 0, "log_bodies": False}
    target = base / "worker.json"
    if not target.exists():
        target.write_text(json.dumps(config, indent=2, ensure_ascii=False), encoding="utf-8")
    return target
