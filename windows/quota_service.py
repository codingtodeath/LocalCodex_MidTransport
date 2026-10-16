import asyncio
import hashlib
import json
import time

from .identity import Identity
from .quota_client import QuotaClient, RPCError
from .quota_normalizer import empty_quota, failed_quota, normalize


class QuotaService:
    def __init__(self, config, state, job=None):
        self.config, self.state, self.job = config, state, job
        self.identity = Identity(config)
        self.client = None
        self.account_digest = None
        self.lock = asyncio.Lock()
        self.last_refresh = float("-inf")
        self.force_refresh = False
        self.state.quota = empty_quota(self.identity.epoch, config.quota.account_alias, self.identity.check,
                                      "mock" if config.enable_mock else "codex_app_server")

    def notification(self, method, params):
        if method == "account/updated" and self.account_digest is not None and self.config.adapter.mode != "existing_codex_login":
            self.identity.invalidate()
        # Limits notifications are not identity evidence. Poll with account read.

    async def refresh(self):
        async with self.lock:
            if time.monotonic() - self.last_refresh < self.config.quota.minimum_refresh_seconds:
                return False
            self.last_refresh = time.monotonic()
            previous_epoch = self.identity.epoch
            self.identity.reload()
            if self.identity.check == "unverified" or previous_epoch != self.identity.epoch:
                self.state.quota = empty_quota(self.identity.epoch, self.config.quota.account_alias, self.identity.check)
            if self.config.enable_mock:
                q = empty_quota(self.identity.epoch, "合成测试账号", "manual_confirmed", "mock")
                self.state.quota = normalize({"rateLimits": {"primary": {"usedPercent": 25, "windowDurationMins": 300, "resetsAt": None}}}, q)
                self.state.quota_supported = True
                return True
            if self.config.quota.mode == "disabled" or not self.config.quota.executable:
                self.state.quota = failed_quota(self.state.quota, "unsupported", "quota_disabled")
                return False
            try:
                if not self.client or self.client.process.returncode is not None:
                    if self.client:
                        await self.client.close()
                    executable = self.config.quota.executable
                    if not self.config.quota.sha256 or hashlib.sha256(executable.read_bytes()).hexdigest() != self.config.quota.sha256:
                        raise ValueError("quota_binary_unverified")
                    self.client = QuotaClient([str(executable), "-c", 'model_provider="openai"',
                        "-c", 'cli_auth_credentials_store="file"', "app-server"], self.config.quota.codex_home,
                        self.config.quota.request_timeout_seconds, self.notification, self.job,
                        external_access=self.config.adapter.mode == "existing_codex_login",
                        outbound_proxy=self.config.adapter.outbound_proxy)
                    await self.client.start()
                if self.config.adapter.mode == "existing_codex_login":
                    from .existing_auth import ExistingAuth
                    access, account_id, login_identity = ExistingAuth(self.config.adapter.existing_auth_file).read()
                    old_epoch = self.identity.epoch
                    self.identity.bind_existing(login_identity)
                    if self.identity.epoch != old_epoch:
                        self.state.quota = empty_quota(self.identity.epoch, self.config.quota.account_alias, self.identity.check)
                    await self.client.use_existing_access({"type": "chatgptAuthTokens", "accessToken": access,
                        "chatgptAccountId": account_id, "chatgptPlanType": None})
                account = await self.client.request("account/read", {"refreshToken": False})
                if account.get("account") is None:
                    self.identity.invalidate()
                    self.state.quota = empty_quota(self.identity.epoch, self.config.quota.account_alias)
                    self.state.quota = failed_quota(self.state.quota, "reauth_required", "quota_login_required")
                    return False
                # In-memory only; never upload the account object, ID, email or digest.
                digest = hashlib.sha256(json.dumps(account.get("account"), sort_keys=True).encode()).hexdigest()
                if self.account_digest is not None and self.account_digest != digest and self.config.adapter.mode != "existing_codex_login":
                    self.identity.invalidate()
                    self.state.quota = empty_quota(self.identity.epoch, self.config.quota.account_alias)
                self.account_digest = digest
                raw = await self.client.request("account/rateLimits/read")
                if self.config.adapter.mode == "existing_codex_login" and ExistingAuth(self.config.adapter.existing_auth_file).identity() != login_identity:
                    self.identity.reload()
                    self.state.quota = empty_quota(self.identity.epoch, self.config.quota.account_alias, self.identity.check)
                    raise ValueError("quota_identity_changed")
                self.state.quota_supported = True
                self.state.quota["identity_check"] = self.identity.check
                self.state.quota = normalize(raw, self.state.quota)
                return self.state.quota["status"] == "ok"
            except RPCError as exc:
                unsupported = exc.code == -32601
                self.state.quota_supported = not unsupported
                self.state.quota = failed_quota(self.state.quota, "unsupported" if unsupported else "unavailable",
                                               "rate_limits_not_supported" if unsupported else "quota_rpc_error")
            except (TimeoutError, ConnectionError, OSError, ValueError, KeyError, AttributeError):
                self.state.quota = failed_quota(self.state.quota, "unavailable", "quota_unavailable")
                if self.client:
                    await self.client.close()
                    self.client = None
            return False

    async def close(self):
        if self.client:
            await self.client.close()
