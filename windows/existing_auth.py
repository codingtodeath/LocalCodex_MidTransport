"""Read the current Codex session locally. Never refresh, copy, or log it."""
import base64
import hashlib
import json
from pathlib import Path


class ExistingAuth:
    def __init__(self, path):
        self.path = Path(path)

    def read(self):
        # Bounded read; no secret-bearing exception includes file contents.
        with self.path.open("rb") as f:
            raw = f.read(65537)
        if len(raw) > 65536:
            raise ValueError("existing_auth_invalid")
        data = json.loads(raw)
        tokens = data.get("tokens") or {}
        if data.get("auth_mode", "chatgpt") != "chatgpt" or data.get("OPENAI_API_KEY"):
            raise ValueError("existing_chatgpt_login_required")
        access, account = tokens.get("access_token"), tokens.get("account_id")
        if not isinstance(access, str) or not access or not isinstance(account, str) or not account:
            raise ValueError("existing_chatgpt_login_required")
        # Identity is stable across access-token rotation and changes on user/workspace switch.
        try:
            segment = access.split(".")[1]
            claims = json.loads(base64.urlsafe_b64decode(segment + "=" * (-len(segment) % 4)))
            user = claims.get("sub")
            if not isinstance(user, str) or not user:
                raise ValueError()
        except (ValueError, IndexError, KeyError):
            raise ValueError("existing_auth_invalid") from None
        identity = hashlib.sha256((user + "\0" + account).encode()).hexdigest()
        return access, account, identity

    def identity(self):
        return self.read()[2]

    def external_params(self):
        access, account, _ = self.read()
        return {"type": "chatgptAuthTokens", "accessToken": access,
                "chatgptAccountId": account, "chatgptPlanType": None}

    def headers(self):
        access, account, _ = self.read()
        return {"Authorization": "Bearer " + access, "ChatGPT-Account-ID": account,
                "Content-Type": "application/json", "Accept-Encoding": "identity",
                "User-Agent": "codex-cli/0.144.1", "originator": "codex_cli_rs"}
