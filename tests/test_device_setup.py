import base64
import hashlib
import json
import zipfile

import pytest

from windows import device_setup


def sample_package(tmp_path, *, extra=None, fingerprint=None):
    public = base64.b64encode(b"synthetic-ed25519-public-key").decode()
    digest = "SHA256:" + base64.b64encode(hashlib.sha256(base64.b64decode(public)).digest()).decode().rstrip("=")
    server = {"host": "relay.example.com", "ssh_port": 22, "ssh_user": "codex-tunnel",
        "host_fingerprint": fingerprint or digest, "worker_id": "workstation",
        "telemetry_url": "https://relay.example.com/internal/v1/telemetry",
        "models_allowlist": ["mock-codex"], "max_concurrency": 4}
    pairing = {"schema_version": 1, "server": server}
    values = {"pairing.json": json.dumps(pairing), "wkr.key": "wkr_" + "w" * 43,
        "tel.key": "tel_" + "t" * 43,
        "relay_ed25519": "-----BEGIN OPENSSH PRIVATE KEY-----\nsynthetic-test-only\n",
        "known_hosts": f"relay.example.com ssh-ed25519 {public}\n"}
    values.update(extra or {})
    path = tmp_path / "device-pairing.zip"
    with zipfile.ZipFile(path, "w") as archive:
        for name, value in values.items():
            archive.writestr(name, value)
    return path, pairing


def test_pairing_is_read_without_extracting_or_copying_oauth(tmp_path):
    path, expected = sample_package(tmp_path)
    data, files = device_setup.read_pairing(path)
    assert data == expected and set(files) == device_setup.MEMBERS
    assert not (tmp_path / "wkr.key").exists()
    assert "auth.json" not in files


@pytest.mark.parametrize("extra", [{"../auth.json": "secret"}, {"auth.json": "secret"}, {"wkr.key": "short"}])
def test_unexpected_or_invalid_private_material_is_rejected(tmp_path, extra):
    path, _ = sample_package(tmp_path, extra=extra)
    with pytest.raises(ValueError):
        device_setup.read_pairing(path)


def test_fingerprint_mismatch_is_rejected(tmp_path):
    path, _ = sample_package(tmp_path, fingerprint="SHA256:wrong")
    with pytest.raises(ValueError, match="fingerprint"):
        device_setup.read_pairing(path)


def test_zip_size_limit_is_checked_before_reading(tmp_path):
    path, _ = sample_package(tmp_path, extra={"known_hosts": "x" * 65537})
    with pytest.raises(ValueError, match="archive"):
        device_setup.read_pairing(path)


def test_config_binds_existing_device_login_without_server_private_paths(tmp_path):
    _, data = sample_package(tmp_path)
    root = tmp_path / "private-root"
    auth = tmp_path / "existing/auth.json"
    binary = tmp_path / "codex.exe"
    binary.write_bytes(b"test-binary")
    c = device_setup.build_config(data, root, auth, binary, "0.144.1", "http://127.0.0.1:7890")
    assert c.adapter.existing_auth_file == auth
    assert c.quota.codex_home != auth.parent
    assert c.quota.sha256 == hashlib.sha256(binary.read_bytes()).hexdigest()
    assert c.ssh.identity_file.is_relative_to(root)
    assert c.bridge.max_concurrency == 4 and c.adapter.safe_retry_budget_seconds == 60
    assert not c.adapter.incident_capture_bodies


def test_duplicate_zip_member_is_rejected(tmp_path):
    path, _ = sample_package(tmp_path)
    with zipfile.ZipFile(path, "a") as archive:
        with pytest.warns(UserWarning):
            archive.writestr("wkr.key", "wkr_" + "z" * 43)
    with pytest.raises(ValueError, match="archive"):
        device_setup.read_pairing(path)


def test_explicit_proxy_cannot_send_authentication_to_remote_proxy():
    with pytest.raises(ValueError, match="loopback"):
        device_setup.detected_proxy("http://proxy.example.com:8080")
    assert device_setup.detected_proxy("direct") is None
