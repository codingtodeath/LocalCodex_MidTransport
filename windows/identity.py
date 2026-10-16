"""Local conservative confirmation binding. Only hashes/epoch are persisted."""
import hashlib
import json
import uuid
from pathlib import Path


def auth_signature(config):
    if config.adapter.mode == "existing_codex_login":
        from .existing_auth import ExistingAuth
        try:
            return ExistingAuth(config.adapter.existing_auth_file).identity()
        except (OSError, ValueError, TypeError):
            return None
    paths = [config.quota.codex_home / "auth.json"]
    proxy_auth = config.adapter.config_file.parent / "proxy-auth"
    if proxy_auth.exists():
        paths += sorted(proxy_auth.glob("*.json"))
    if len(paths) != 2 or any(not p.is_file() for p in paths):
        return None
    digests = [hashlib.sha256(p.read_bytes()).hexdigest() for p in paths]
    return hashlib.sha256("|".join(digests).encode()).hexdigest()


class Identity:
    def __init__(self, config):
        self.config = config
        self.path = config.runtime_dir / "identity.json"
        self.epoch = str(uuid.uuid4())
        self.check = "unverified"
        self.reload()

    def reload(self):
        if self.config.adapter.mode == "existing_codex_login":
            signature = auth_signature(self.config)
            if signature:
                self.bind_existing(signature)
                return
        try:
            marker = json.loads(self.path.read_text(encoding="utf-8"))
            if marker.get("signature") and marker["signature"] == auth_signature(self.config):
                self.epoch, self.check = marker["epoch"], "automatic_match" if self.config.adapter.mode == "existing_codex_login" else "manual_confirmed"
                return
        except (OSError, ValueError, KeyError):
            pass
        if self.config.adapter.mode == "existing_codex_login":
            signature = auth_signature(self.config)
            if signature:
                self.epoch, self.check = str(uuid.uuid4()), "automatic_match"
                self.path.write_text(json.dumps({"signature": signature, "epoch": self.epoch}), encoding="utf-8")
                return
        if self.check != "unverified":
            self.epoch = str(uuid.uuid4())
            self.path.unlink(missing_ok=True)
        self.check = "unverified"

    def bind_existing(self, signature):
        try:
            marker = json.loads(self.path.read_text(encoding="utf-8"))
            if marker.get("signature") == signature:
                self.epoch, self.check = marker["epoch"], "automatic_match"
                return
        except (OSError, ValueError, KeyError):
            pass
        self.epoch, self.check = str(uuid.uuid4()), "automatic_match"
        self.path.write_text(json.dumps({"signature": signature, "epoch": self.epoch}), encoding="utf-8")

    def invalidate(self):
        self.check, self.epoch = "unverified", str(uuid.uuid4())
        self.path.unlink(missing_ok=True)

    def confirm(self):
        signature = auth_signature(self.config)
        if not signature:
            raise ValueError("two_independent_logins_required")
        self.epoch, self.check = str(uuid.uuid4()), "manual_confirmed"
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.path.write_text(json.dumps({"signature": signature, "epoch": self.epoch}), encoding="utf-8")
