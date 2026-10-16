import argparse
import asyncio
import hashlib
import json
import logging
import os
import signal
import time
from pathlib import Path
import uvicorn
from windows.bridge import create_app
from windows.config import read_key
from windows.existing_adapter import ExistingAdapter
from windows.identity import Identity
from windows.quota_normalizer import empty_quota, normalize, failed_quota
from windows.runtime_diagnostics import diagnostics, exception_info
from windows.state import State, utcnow
from windows.telemetry import Commands, Telemetry
from .auth import ManagedAuth
from .config import LinuxConfig
from .live_limits import apply_worker_limits


class ReloadingTelemetry(Telemetry):
    async def send(self):
        # Credential rotation does not interrupt inference or require a restart.
        self.key = read_key(self.config.telemetry.secret_file, "tel_")
        return await super().send()


class ManagedAdapter(ExistingAdapter):
    def __init__(self, config, state, manager):
        super().__init__(config, state)
        self.manager = manager

    async def open(self, method, path, body=None, request_headers=None):
        await self.manager.ensure()
        signature = hashlib.sha256(self.config.adapter.existing_auth_file.read_bytes()).digest()
        response = await super().open(method, path, body, request_headers)
        # Only a definite 401 rejection is replayed, never a received stream or 403.
        if response.status_code == 401:
            await response.aclose()
            await self.manager.ensure(force=True, rejected_signature=signature)
            await self.probe()
            response = await super().open(method, path, body, request_headers)
        return response


class ManagedQuota:
    def __init__(self, config, state, manager):
        self.config, self.state, self.manager = config, state, manager
        self.identity = Identity(config)
        self.last_refresh = float("-inf")
        self.force_refresh = False
        state.quota = empty_quota(self.identity.epoch, config.quota.account_alias, self.identity.check)

    async def refresh(self):
        if time.monotonic() - self.last_refresh < self.config.quota.minimum_refresh_seconds:
            return False
        self.last_refresh = time.monotonic()
        try:
            self.identity.reload()
            raw = await self.manager.limits()
            previous = self.state.quota
            if previous["account_epoch"] != self.identity.epoch:
                previous = empty_quota(self.identity.epoch, self.config.quota.account_alias, self.identity.check)
            self.state.quota = normalize(raw, previous)
            self.state.quota_supported = True
            return self.state.quota["status"] == "ok"
        except Exception:
            self.state.quota = failed_quota(self.state.quota, "unavailable", "quota_unavailable")
            return False


def write_status(config, state, running=True):
    value = {"running": running, "platform": "linux", "instance_id": state.instance_id,
        "observed_at": utcnow(), "local": state.local(), "telemetry_ok": state.telemetry_ok,
        "quota_status": (state.quota or {}).get("status"), "quota": state.quota,
        "activated": config.activation_file.exists(), "diagnostic_log": {"enabled": True,
        "incident_body_capture": config.adapter.incident_capture_bodies}}
    target = config.runtime_dir / "status.json"
    temp = target.with_suffix(".tmp")
    temp.write_text(json.dumps(value), encoding="utf-8")
    os.replace(temp, target)


async def run(config):
    os.umask(0o077)
    state = State(config.worker_id)
    diag = diagnostics(config, state)
    manager = ManagedAuth(config, diag)
    quota = ManagedQuota(config, state, manager)
    commands = Commands(config.runtime_dir / "commands.sqlite")
    telemetry = ReloadingTelemetry(config, state, commands)
    adapter = ManagedAdapter(config, state, manager)
    app = create_app(config, state, adapter)
    for name in ("uvicorn.error", "uvicorn.access", "httpx", "httpcore"):
        logging.getLogger(name).disabled = True
    server = uvicorn.Server(uvicorn.Config(app, host="127.0.0.1", port=8765,
        log_config=None, access_log=False, proxy_headers=False,
        timeout_graceful_shutdown=1800, h11_max_incomplete_event_size=65536, limit_concurrency=128))
    server.install_signal_handlers = lambda: None
    stop = asyncio.Event()
    refresh_requested = asyncio.Event()
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGTERM, signal.SIGINT):
        loop.add_signal_handler(sig, stop.set)
    loop.add_signal_handler(signal.SIGUSR1, refresh_requested.set)
    diag.emit("worker_started", instance_id=state.instance_id, process_id=os.getpid())

    async def control_loop():
        while True:
            apply_worker_limits(config)
            try:
                force = refresh_requested.is_set()
                refresh_requested.clear()
                await manager.ensure(force=force)
            except Exception as exc:
                diag.check("credentials", False, **exception_info(exc))
            await adapter.probe()
            write_status(config, state)
            await asyncio.sleep(2)

    async def quota_loop():
        due, failures = 0, 0
        while True:
            await commands.execute(quota)
            if time.monotonic() >= due or quota.force_refresh:
                quota.force_refresh = False
                ok = await quota.refresh()
                diag.check("quota", ok, error_code="ok" if ok else "quota_unavailable")
                failures = 0 if ok else failures + 1
                due = time.monotonic() + (config.quota.poll_seconds if ok else min(900, config.quota.poll_seconds * 2 ** min(failures-1, 3)))
            await asyncio.sleep(1)

    async def telemetry_loop():
        while True:
            if config.activation_file.exists():
                await telemetry.send()
                await asyncio.sleep(config.telemetry.interval_seconds)
            else:
                await asyncio.sleep(.1)

    tasks = [asyncio.create_task(server.serve(), name="bridge_server"),
        asyncio.create_task(control_loop(), name="control_loop"),
        asyncio.create_task(quota_loop(), name="quota_loop"),
        asyncio.create_task(telemetry_loop(), name="telemetry_loop")]
    waiter = asyncio.create_task(stop.wait())
    try:
        done, _ = await asyncio.wait([*tasks, waiter], return_when=asyncio.FIRST_COMPLETED)
        for task in done:
            if task is not waiter:
                diag.emit("worker_component_failed", component=task.get_name(),
                    **exception_info(task.exception() or RuntimeError()))
                raise RuntimeError("worker_component_stopped")
    finally:
        diag.emit("worker_stopping")
        server.should_exit = True
        for task in tasks[1:]:
            task.cancel()
        waiter.cancel()
        await asyncio.gather(*tasks[1:], waiter, return_exceptions=True)
        await tasks[0]
        await manager.close()
        await telemetry.close()
        commands.close()
        state.bridge_ready = state.adapter_ready = False
        write_status(config, state, False)
        diag.emit("worker_stopped")


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--config", required=True)
    args = parser.parse_args()
    config = LinuxConfig.model_validate(json.loads(Path(args.config).read_text()))
    asyncio.run(run(config))


if __name__ == "__main__":
    main()
