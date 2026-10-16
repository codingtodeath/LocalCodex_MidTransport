"""One official app-server owns this server's OAuth refresh lifecycle."""
import asyncio
import base64
import hashlib
import json
import time
from windows.existing_auth import ExistingAuth
from windows.quota_client import QuotaClient
from windows.runtime_diagnostics import exception_info


def remaining_seconds(path):
    access = ExistingAuth(path).read()[0]
    segment = access.split(".")[1]
    claims = json.loads(base64.urlsafe_b64decode(segment + "=" * (-len(segment) % 4)))
    return float(claims["exp"]) - time.time()


class ManagedAuth:
    def __init__(self, config, diagnostics, client_factory=QuotaClient):
        self.config, self.diagnostics = config, diagnostics
        self.client_factory = client_factory
        self.client = None
        self.lock = asyncio.Lock()
        self.last_forced = float("-inf")
        self.identity = ExistingAuth(config.adapter.existing_auth_file).identity()

    async def _start(self):
        if self.client and self.client.process.returncode is None:
            return
        if self.client:
            await self.client.close()
        binary = self.config.quota.executable
        if hashlib.sha256(binary.read_bytes()).hexdigest() != self.config.quota.sha256:
            raise ValueError("quota_binary_unverified")
        self.client = self.client_factory([str(binary), "-c", 'model_provider="openai"',
            "-c", 'cli_auth_credentials_store="file"', "app-server"],
            self.config.quota.codex_home, self.config.quota.request_timeout_seconds,
            external_access=False, outbound_proxy=self.config.adapter.outbound_proxy)
        await self.client.start()

    async def _account(self, force=False):
        await self._start()
        refresh = force or remaining_seconds(self.config.adapter.existing_auth_file) < 600
        # A failed refresh is retried at most once per minute, across all sessions.
        if refresh and time.monotonic() - self.last_forced < 60:
            if remaining_seconds(self.config.adapter.existing_auth_file) <= 0:
                raise ValueError("credential_expired")
            return
        if refresh:
            self.last_forced = time.monotonic()
            self.diagnostics.emit("credential_refresh_started")
        try:
            result = await self.client.request("account/read", {"refreshToken": refresh})
            if not result.get("account") or result["account"].get("type") != "chatgpt":
                raise ValueError("managed_login_required")
            if ExistingAuth(self.config.adapter.existing_auth_file).identity() != self.identity:
                raise ValueError("managed_account_changed")
            if refresh:
                self.diagnostics.emit("credential_refresh_finished")
            self.diagnostics.observe_auth(self.config.adapter.existing_auth_file)
        except Exception as exc:
            if refresh:
                self.diagnostics.emit("credential_refresh_failed", **exception_info(exc))
            await self.client.close()
            self.client = None
            raise

    async def ensure(self, force=False, rejected_signature=None):
        async with self.lock:
            if rejected_signature is not None and hashlib.sha256(self.config.adapter.existing_auth_file.read_bytes()).digest() != rejected_signature:
                return
            if force or remaining_seconds(self.config.adapter.existing_auth_file) < 600:
                await self._account(force)

    async def limits(self):
        async with self.lock:
            await self._account()
            return await self.client.request("account/rateLimits/read")

    async def close(self):
        if self.client:
            await self.client.close()
