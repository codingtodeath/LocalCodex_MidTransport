"""Import private pairing material; read this device's existing Codex login only."""
import argparse
import base64
import hashlib
import json
import os
import re
import secrets
import shutil
import socket
import subprocess
import sys
import urllib.request
import zipfile
from pathlib import Path
from urllib.parse import urlsplit

from .config import Config, load_config
from .existing_auth import ExistingAuth
from .security import secure_dir

MEMBERS = {"pairing.json", "wkr.key", "tel.key", "relay_ed25519", "known_hosts"}


def read_pairing(package):
    with zipfile.ZipFile(package) as archive:
        infos = archive.infolist()
        if (len(infos) != len(MEMBERS) or {i.filename for i in infos} != MEMBERS
                or any(i.file_size > 65536 or i.is_dir() for i in infos)):
            raise ValueError("invalid_pairing_archive")
        files = {i.filename: archive.read(i) for i in infos}
    data = json.loads(files["pairing.json"])
    if data.get("schema_version") != 1:
        raise ValueError("unsupported_pairing_version")
    for name, prefix in [("wkr.key", "wkr_"), ("tel.key", "tel_")]:
        if not re.fullmatch(re.escape(prefix) + r"[A-Za-z0-9_-]{43,}", files[name].decode().strip()):
            raise ValueError("invalid_pairing_key")
    if not files["relay_ed25519"].startswith(b"-----BEGIN OPENSSH PRIVATE KEY-----"):
        raise ValueError("dedicated_ssh_key_required")
    server = data["server"]
    host, port = server["host"], server.get("ssh_port", 22)
    target = host if port == 22 else f"[{host}]:{port}"
    rows = [r.split() for r in files["known_hosts"].decode().splitlines() if r.strip() and not r.startswith("#")]
    if len(rows) != 1 or len(rows[0]) < 3 or rows[0][0] != target or rows[0][1] != "ssh-ed25519":
        raise ValueError("invalid_dedicated_known_hosts")
    digest = hashlib.sha256(base64.b64decode(rows[0][2], validate=True)).digest()
    actual = "SHA256:" + base64.b64encode(digest).decode().rstrip("=")
    if actual != server["host_fingerprint"]:
        raise ValueError("ssh_host_fingerprint_mismatch")
    origin = urlsplit(server["telemetry_url"])
    if origin.hostname != host or origin.port not in (None, 443):
        raise ValueError("pairing_server_mismatch")
    return data, files


def file_sha256(path):
    with Path(path).open("rb") as source:
        return hashlib.file_digest(source, "sha256").hexdigest()


def codex_candidates(explicit=None):
    if explicit:
        yield Path(explicit)
        return
    native = shutil.which("codex.exe")
    if native:
        yield Path(native)
    wrapper = shutil.which("codex.cmd")
    roots = [Path(wrapper).parent / "node_modules/@openai/codex"] if wrapper else []
    roots.append(Path(os.environ.get("APPDATA", "")) / "npm/node_modules/@openai/codex")
    for root in roots:
        yield from root.glob("node_modules/@openai/codex-win32-x64/vendor/*/bin/codex.exe")
    local = Path(os.environ.get("LOCALAPPDATA", ""))
    for root in [local / "Programs/Codex/resources", local / "Codex/app/resources"]:
        yield root / "codex.exe"
    if os.name == "nt":
        # Query just the installed Codex package, without walking WindowsApps.
        result = subprocess.run(["powershell.exe", "-NoProfile", "-NonInteractive", "-Command",
            "Get-AppxPackage -Name '*Codex*' | Select-Object -ExpandProperty InstallLocation"],
            capture_output=True, text=True, timeout=15, creationflags=subprocess.CREATE_NO_WINDOW)
        for row in result.stdout.splitlines():
            if row.strip():
                yield Path(row.strip()) / "app/resources/codex.exe"
                yield Path(row.strip()) / "resources/codex.exe"


def discover_codex(explicit=None):
    seen = set()
    for candidate in codex_candidates(explicit):
        if not candidate.is_file() or candidate in seen:
            continue
        seen.add(candidate)
        try:
            result = subprocess.run([str(candidate), "--version"], capture_output=True, timeout=15,
                creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0)
            match = re.search(rb"codex-cli (\d+\.\d+\.\d+)", result.stdout)
            if result.returncode == 0 and match:
                return candidate.resolve(), match[1].decode()
        except (OSError, subprocess.TimeoutExpired):
            continue
    raise ValueError("codex_native_binary_not_found")


def detected_proxy(explicit=None):
    if explicit == "direct":
        return None
    candidates = [explicit] if explicit else [os.environ.get("HTTPS_PROXY"), urllib.request.getproxies().get("https")]
    for candidate in candidates:
        if not candidate:
            continue
        parsed = urlsplit(candidate if "://" in candidate else "http://" + candidate)
        if (parsed.scheme != "http" or parsed.hostname != "127.0.0.1" or not parsed.port
                or parsed.username or parsed.password or parsed.path not in ("", "/") or parsed.query or parsed.fragment):
            if explicit:
                raise ValueError("loopback_http_proxy_required")
            continue
        try:
            with socket.create_connection(("127.0.0.1", parsed.port), timeout=1):
                return f"http://127.0.0.1:{parsed.port}"
        except OSError:
            if explicit:
                raise ValueError("local_proxy_not_listening") from None
    return None


def build_config(pairing, root, auth_path, binary, version, proxy):
    server = pairing["server"]
    diagnostics = pairing.get("diagnostics", {})
    return Config.model_validate({
        "environment": "production", "worker_id": server["worker_id"],
        "runtime_dir": str(root / "runtime"),
        "bridge": {"auth_secret_file": str(root / "secrets/wkr.key"),
            "max_concurrency": server.get("max_concurrency", 4),
            "connect_seconds": 60, "idle_seconds": 300, "total_seconds": 1800},
        "adapter": {"mode": "existing_codex_login", "existing_auth_file": str(auth_path),
            "outbound_proxy": proxy, "api_key_file": str(root / "secrets/cpa.key"),
            "config_file": str(root / "proxy-config.yaml"),
            "safe_retry_budget_seconds": 60,
            "cache_diagnostics_capture_inputs": diagnostics.get("capture_request_inputs", False),
            "incident_capture_bodies": diagnostics.get("capture_incident_bodies", False)},
        "quota": {"codex_home": str(root / "quota-codex"), "executable": str(binary),
            "pinned_version": version, "sha256": file_sha256(binary)},
        "telemetry": {"url": server["telemetry_url"], "secret_file": str(root / "secrets/tel.key")},
        "ssh": {"host": server["host"], "port": server.get("ssh_port", 22),
            "user": server.get("ssh_user", "codex-tunnel"),
            "identity_file": str(root / "ssh/relay_ed25519"),
            "known_hosts_file": str(root / "ssh/known_hosts")},
        "models_allowlist": server["models_allowlist"]})


def install(package, *, root=None, auth_path=None, codex_executable=None, proxy=None, replace=False):
    if os.name != "nt":
        raise ValueError("windows_required")
    root = Path(root or Path(os.environ["LOCALAPPDATA"]) / "CodexRelay").resolve()
    if "onedrive" in str(root).lower():
        raise ValueError("synced_secret_directory_rejected")
    target = root / "worker.json"
    if target.exists():
        from .local_control import is_running
        if is_running(load_config(target)):
            raise ValueError("stop_worker_before_pairing")
        if not replace:
            raise ValueError("already_configured_use_replace")
    pairing, files = read_pairing(package)
    auth_path = Path(auth_path or Path(os.environ.get("CODEX_HOME", Path.home() / ".codex")) / "auth.json").resolve()
    ExistingAuth(auth_path).read()  # Verify only. Never copy or print the login.
    binary, version = discover_codex(codex_executable)
    config = build_config(pairing, root, auth_path, binary, version, detected_proxy(proxy))
    # Validate every input before replacing any existing private files.
    secure_dir(root)
    for name in ["runtime", "secrets", "ssh", "logs", "quota-codex"]:
        secure_dir(root / name)
    if target.exists():
        import datetime
        backup = root / "pairing-backups" / datetime.datetime.now().strftime("%Y%m%d-%H%M%S-%f")
        secure_dir(backup)
        shutil.copy2(target, backup / "worker.json")
        for name in ["secrets", "ssh"]:
            shutil.copytree(root / name, backup / name)
    for source, dest in [("wkr.key", "secrets/wkr.key"), ("tel.key", "secrets/tel.key"),
                         ("relay_ed25519", "ssh/relay_ed25519"), ("known_hosts", "ssh/known_hosts")]:
        (root / dest).write_bytes(files[source])
    if not (root / "secrets/cpa.key").exists():
        (root / "secrets/cpa.key").write_text("cpa_" + secrets.token_urlsafe(32), encoding="utf-8")
    (root / "quota-codex/config.toml").write_text('model_provider = "openai"\ncli_auth_credentials_store = "file"\n', encoding="utf-8")
    (root / "runtime/server-migration.json").unlink(missing_ok=True)
    target.write_text(json.dumps(config.model_dump(mode="json"), ensure_ascii=False, indent=2), encoding="utf-8")
    return {"configured": True, "config_file": str(target), "codex_version": version,
        "using_existing_login": True, "oauth_copied": False, "outbound_proxy_configured": bool(config.adapter.outbound_proxy)}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--package", type=Path)
    parser.add_argument("--auth-file", type=Path)
    parser.add_argument("--codex-executable", type=Path)
    parser.add_argument("--proxy", help="Loopback HTTP proxy, or 'direct'")
    parser.add_argument("--replace", action="store_true")
    args = parser.parse_args()
    if not args.package:
        import tkinter as tk
        from tkinter.filedialog import askopenfilename
        root = tk.Tk(); root.withdraw()
        chosen = askopenfilename(title="选择私下收到的设备配对 ZIP", filetypes=[("Pairing ZIP", "*.zip")])
        root.destroy()
        if not chosen:
            print('{"configured":false,"error":"pairing_selection_cancelled"}'); return 1
        args.package = Path(chosen)
    try:
        result = install(args.package, auth_path=args.auth_file, codex_executable=args.codex_executable,
            proxy=args.proxy, replace=args.replace)
        print(json.dumps(result, ensure_ascii=True))
        return 0
    except (OSError, ValueError, KeyError, zipfile.BadZipFile, subprocess.SubprocessError) as exc:
        allowed = {"windows_required", "stop_worker_before_pairing", "already_configured_use_replace",
            "codex_native_binary_not_found", "local_proxy_not_listening", "loopback_http_proxy_required",
            "synced_secret_directory_rejected", "ssh_host_fingerprint_mismatch"}
        code = str(exc) if type(exc) is ValueError and str(exc) in allowed else "pairing_or_local_login_invalid"
        print(json.dumps({"configured": False, "error": code}, ensure_ascii=True))
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
