import asyncio
import json
import os
import subprocess
from contextlib import suppress

ALLOWED = {"initialize", "account/read", "account/rateLimits/read", "account/usage/read"}


class RPCError(Exception):
    def __init__(self, code):
        super().__init__("quota_rpc_error")
        self.code = code


def quota_environment(home, outbound_proxy=None, isolate_proxy=False):
    env = {k: v for k, v in os.environ.items() if not k.upper().startswith(("OPENAI_", "CODEX_", "CPA_", "MANAGEMENT_"))}
    env["CODEX_HOME"] = str(home)
    if isolate_proxy:
        env = {k: v for k, v in env.items() if k.upper() not in ("HTTP_PROXY", "HTTPS_PROXY", "ALL_PROXY", "NO_PROXY")}
        if outbound_proxy:
            env["HTTP_PROXY"] = env["HTTPS_PROXY"] = outbound_proxy
        env["NO_PROXY"] = "127.0.0.1,localhost"
    return env


class QuotaClient:
    def __init__(self, command, home, timeout=15, notification=None, job=None, external_access=False, outbound_proxy=None):
        self.command, self.home, self.timeout = command, home, timeout
        self.notification, self.job = notification, job
        self.process = None
        self.pending = {}
        self.counter = 0
        self.reader = None
        self.write_lock = asyncio.Lock()
        self.external_access = external_access
        self.outbound_proxy = outbound_proxy

    async def start(self):
        # Stderr is discarded, never interpolated into an error or sent in telemetry.
        self.process = await asyncio.create_subprocess_exec(*self.command, cwd=str(self.home),
            env=quota_environment(self.home, self.outbound_proxy, self.external_access), stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.DEVNULL,
            limit=1024 * 1024,
            creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0)
        if self.job:
            self.job.assign(self.process.pid)
        self.reader = asyncio.create_task(self._read())
        try:
            params = {"clientInfo": {"name": "codex_private_relay_quota", "version": "0.1.0"}}
            if self.external_access:
                params["capabilities"] = {"experimentalApi": True}
            await self.request("initialize", params)
            await self._send({"method": "initialized", "params": {}})
        except BaseException:
            await self.close()
            raise

    async def _send(self, obj):
        async with self.write_lock:
            self.process.stdin.write((json.dumps(obj, separators=(",", ":")) + "\n").encode())
            await self.process.stdin.drain()

    async def request(self, method, params=None):
        if method not in ALLOWED:
            raise ValueError("quota_method_not_allowed")
        return await self._request(method, params)

    async def use_existing_access(self, params):
        # Local, access-token-only ephemeral mode. Never accept interactive login,
        # API key login, refresh tokens, or caller-controlled RPC from the network.
        if not self.external_access or set(params) != {"type", "accessToken", "chatgptAccountId", "chatgptPlanType"} or params["type"] != "chatgptAuthTokens":
            raise ValueError("external_access_only")
        return await self._request("account/login/start", params)

    async def _request(self, method, params=None):
        self.counter += 1
        ident = self.counter
        future = asyncio.get_running_loop().create_future()
        self.pending[ident] = future
        message = {"id": ident, "method": method}
        if params is not None:
            message["params"] = params
        try:
            await self._send(message)
            return await asyncio.wait_for(future, self.timeout)
        finally:
            self.pending.pop(ident, None)

    async def _read(self):
        try:
            while line := await self.process.stdout.readline():
                obj = json.loads(line)
                if not isinstance(obj, dict):
                    continue
                if "method" in obj:
                    if "id" in obj:
                        # Refuse server requests such as token refresh, approvals, tools.
                        await self._send({"id": obj["id"], "error": {"code": -32601, "message": "Unsupported method"}})
                    elif obj["method"] in ("account/updated", "account/rateLimits/updated") and self.notification:
                        self.notification(obj["method"], obj.get("params", {}))
                    continue
                future = self.pending.get(obj.get("id"))
                if future and not future.done():
                    if "error" in obj:
                        future.set_exception(RPCError(obj["error"].get("code")))
                    elif "result" in obj:
                        future.set_result(obj["result"])
        except (ValueError, OSError, asyncio.LimitOverrunError):
            pass
        finally:
            for f in self.pending.values():
                if not f.done():
                    f.set_exception(ConnectionError("quota_disconnected"))

    async def close(self):
        if self.process and self.process.returncode is None:
            self.process.terminate()
            try:
                await asyncio.wait_for(self.process.wait(), 5)
            except TimeoutError:
                self.process.kill()
                await self.process.wait()
        if self.reader:
            self.reader.cancel()
            with suppress(asyncio.CancelledError):
                await self.reader
