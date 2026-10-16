import argparse
import asyncio
import getpass
import importlib.metadata
import json
import os
import platform
import re
import subprocess
import sys
import time
from pathlib import Path

from .config import load_config, read_key
from .identity import Identity
from .processes import InstanceLock, port_available
from .quota_client import quota_environment
from .security import strict_acl
from .setup import setup, ROOT
from .supervisor import run


def is_running(config):
    lock = InstanceLock(config.runtime_dir / "worker.lock")
    try:
        lock.acquire()
    except RuntimeError:
        return True
    lock.release()
    return False


def version(command, pattern):
    try:
        result = subprocess.run(command, capture_output=True, timeout=10)
        data = (result.stdout + result.stderr).decode("utf-8", errors="replace")
        found = re.search(pattern, data)
        return found.group(1) if found else None
    except (OSError, subprocess.TimeoutExpired):
        return None


def doctor(config):
    secrets_valid = {}
    for prefix, path in (("wkr", config.bridge.auth_secret_file), ("cpa", config.adapter.api_key_file),
                         ("tel", config.telemetry.secret_file)):
        try:
            read_key(path, prefix + "_")
            secrets_valid[prefix] = True
        except (OSError, ValueError):
            secrets_valid[prefix] = False
    from .supervisor import check_proxy
    try:
        if config.adapter.mode == "existing_codex_login":
            from .existing_auth import ExistingAuth
            ExistingAuth(config.adapter.existing_auth_file).read()
        else:
            check_proxy(config)
        pinned_adapter = True
    except (OSError, ValueError):
        pinned_adapter = False
    dirs = {name: (config.runtime_dir.parent / name) for name in ("secrets", "proxy-auth", "quota-codex", "runtime", "logs", "ssh")}
    return {"os": platform.platform(), "python": platform.python_version(),
        "packages": {name: importlib.metadata.version(name) for name in ("fastapi", "httpx", "pydantic", "uvicorn", "jsonschema", "PyYAML")},
        "openssh": version([str(config.ssh.executable), "-V"], r"(OpenSSH[^,\r\n]+)"),
        "codex": version([str(config.quota.executable), "--version"], r"codex-cli ([0-9.]+)") if config.quota.executable else None,
        "cliproxyapi": version([str(config.adapter.executable), "-help"], r"CLIProxyAPI Version: ([0-9.]+)") if config.adapter.executable else None,
        "adapter_pin_and_configuration_valid": pinned_adapter,
        "ports_available": {str(p): port_available(p) for p in (8317, 8765)},
        "directories": {name: {"exists": p.is_dir(), "writable": os.access(p, os.W_OK),
                                "strict_acl": p.exists() and strict_acl(p)} for name, p in dirs.items()},
        "secrets_valid": secrets_valid, "ssh_identity_present": config.ssh.identity_file.is_file(),
        "ssh_known_hosts_present": config.ssh.known_hosts_file.is_file(), "telemetry_configured": bool(config.telemetry.url),
        "ca_present": bool(config.telemetry.ca_cert_file and config.telemetry.ca_cert_file.is_file()),
        "tls_trust": "explicit_ca" if config.telemetry.ca_cert_file else "default_trust",
        "login_mode": config.adapter.mode,
        "identity_confirmed": Identity(config).check in ("manual_confirmed", "automatic_match"), "mock": config.enable_mock,
        "running": is_running(config)}


def default_config():
    return Path(os.environ.get("LOCALAPPDATA", ".")) / "CodexRelay/worker.json"


def launch_worker(config_path):
    if os.name == "nt":
        # The OS task manager owns the long-lived process. A process spawned
        # directly by Codex may inherit its outer kill-on-close Job even when
        # CREATE_NO_WINDOW / CREATE_BREAKAWAY_FROM_JOB are requested.
        shell = str(Path(os.environ.get("SystemRoot", "C:/Windows")) / "System32/WindowsPowerShell/v1.0/powershell.exe")
        result = subprocess.run([shell, "-NoProfile", "-NonInteractive", "-ExecutionPolicy", "Bypass",
            "-File", str(ROOT / "deploy/windows/Start-DetachedWorker.ps1"),
            "-Python", sys.executable, "-Config", str(config_path.resolve())],
            cwd=ROOT, capture_output=True, timeout=20, creationflags=subprocess.CREATE_NO_WINDOW)
        if result.returncode: raise RuntimeError("worker_detached_start_failed")
        return None
    return subprocess.Popen([sys.executable, "-m", "windows.local_control", "run", "--config", str(config_path.resolve())],
        cwd=ROOT, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
        start_new_session=True)


def main():
    parser = argparse.ArgumentParser(description="Private relay local control; no credential output")
    parser.add_argument("command", choices=["setup", "doctor", "run", "start", "stop", "status", "confirm-identity", "set-key", "use-existing-login"])
    parser.add_argument("--config", type=Path, default=default_config())
    parser.add_argument("--mock", action="store_true")
    parser.add_argument("--key-role", choices=["wkr", "tel"])
    parser.add_argument("--same-account-and-workspace", action="store_true")
    args = parser.parse_args()
    try:
        if args.command == "setup":
            path = setup(mock=args.mock)
            print(json.dumps({"setup": "ok", "config_file": str(path)}, ensure_ascii=False))
            return
        config = load_config(args.config)
        if args.command in {"start", "run"} and (config.runtime_dir / "server-migration.json").exists():
            raise ValueError("worker_migrated_to_server")
        if args.command == "use-existing-login":
            if config.enable_mock or is_running(config):
                raise ValueError("stop_real_worker_before_switching_login")
            path = Path(os.environ.get("CODEX_HOME", str(Path.home() / ".codex"))) / "auth.json"
            from .existing_auth import ExistingAuth
            ExistingAuth(path).read()
            config.adapter.mode, config.adapter.existing_auth_file = "existing_codex_login", path.resolve()
            from urllib.parse import urlsplit
            candidate = os.environ.get("HTTPS_PROXY", "")
            u = urlsplit(candidate)
            if u.scheme == "http" and u.hostname == "127.0.0.1" and u.port and not u.username and not u.password:
                config.adapter.outbound_proxy = f"http://127.0.0.1:{u.port}"
            # Revalidate all boundaries before saving the reversible config change.
            from .config import Config
            Config.model_validate(config.model_dump())
            args.config.write_text(json.dumps(config.model_dump(mode="json"), ensure_ascii=False, indent=2), encoding="utf-8")
            Identity(config).reload()
            print('{"login":"existing_codex_session","extra_logins":0,"refresh_owner":"existing_codex","credentials_copied":false}')
        elif args.command == "doctor":
            print(json.dumps(doctor(config), ensure_ascii=False, indent=2))
        elif args.command == "status":
            status = {"running": is_running(config)}
            target = config.runtime_dir / "status.json"
            if target.is_file():
                status.update(json.loads(target.read_text(encoding="utf-8")))
                status["running"] = is_running(config)
            print(json.dumps(status, ensure_ascii=False, indent=2))
        elif args.command == "run":
            asyncio.run(run(config))
        elif args.command == "start":
            if is_running(config):
                print('{"start":"already_running"}')
                return
            proc = launch_worker(args.config)
            deadline = time.monotonic() + 10
            while time.monotonic() < deadline and (proc is None or proc.poll() is None):
                status_file = config.runtime_dir / "status.json"
                if status_file.is_file() and json.loads(status_file.read_text(encoding="utf-8")).get("local", {}).get("bridge_ready") and is_running(config):
                    print('{"start":"started"}')
                    return
                time.sleep(.2)
            print('{"start":"failed_or_pending"}')
            sys.exit(1)
        elif args.command == "stop":
            if is_running(config):
                (config.runtime_dir / "stop.request").write_text("stop", encoding="ascii")
                deadline = time.monotonic() + 20
                while is_running(config) and time.monotonic() < deadline:
                    time.sleep(.2)
                if is_running(config):
                    raise RuntimeError("worker_stop_pending")
            print('{"stop":"stopped"}')
        elif args.command == "confirm-identity":
            if is_running(config):
                raise ValueError("stop_worker_before_identity_confirmation")
            if not args.same_account_and_workspace:
                raise ValueError("explicit_local_identity_confirmation_required")
            Identity(config).confirm()
            print('{"identity":"manual_confirmed"}')
        elif args.command == "set-key":
            if is_running(config) or not args.key_role:
                raise ValueError("stop_worker_and_select_key_role")
            value = getpass.getpass("Paste service key in this local terminal (hidden): ").strip()
            if not re.fullmatch(args.key_role + r"_[A-Za-z0-9_-]{43}", value):
                raise ValueError("invalid_key_format")
            target = config.bridge.auth_secret_file if args.key_role == "wkr" else config.telemetry.secret_file
            target.write_text(value, encoding="utf-8")
            print('{"key":"saved"}')
    except KeyboardInterrupt:
        print('{"worker":"stopped"}')
    except Exception as exc:
        # Config validation / HTTP / upstream exceptions can contain sensitive values.
        allowed = {"worker_already_running", "required_port_in_use", "adapter_binary_unverified", "adapter_configuration_changed",
            "single_proxy_account_required", "ssh_trust_or_identity_missing", "worker_component_stopped", "secret_root_acl_not_strict",
            "worker_stop_pending", "worker_detached_start_failed", "stop_worker_before_identity_confirmation", "explicit_local_identity_confirmation_required",
            "two_independent_logins_required", "stop_worker_and_select_key_role", "invalid_key_format", "acl_setup_failed", "worker_migrated_to_server"}
        code = str(exc) if str(exc) in allowed else "local_operation_failed"
        print(json.dumps({"error": code, "hint": "check doctor and local setup"}))
        sys.exit(1)


if __name__ == "__main__":
    main()
