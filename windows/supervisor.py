import asyncio
import hashlib
import json
import logging
import os
import time
from pathlib import Path

import uvicorn
import yaml

from .bridge import create_app
from .config import read_key
from .mock_upstream import create_mock
from .processes import InstanceLock, ManagedProcess, port_available
from .quota_client import quota_environment
from .quota_service import QuotaService
from .state import State, utcnow
from .telemetry import Commands, Telemetry
from .tunnel import ssh_command
from .winjob import Job
from .runtime_diagnostics import diagnostics, exception_info


def check_proxy(config):
    """Fail closed if binary or generated configuration has changed."""
    a = config.adapter
    if not a.executable or not a.sha256 or hashlib.sha256(a.executable.read_bytes()).hexdigest() != a.sha256:
        raise ValueError("adapter_binary_unverified")
    document = yaml.safe_load(a.config_file.read_text(encoding="utf-8"))
    # Compare against the complete minimal config; reject hidden providers/retries.
    from .setup import proxy_document
    if document != proxy_document(a.config_file.parent, read_key(a.api_key_file, "cpa_")):
        raise ValueError("adapter_configuration_changed")
    auth_dir = a.config_file.parent / "proxy-auth"
    if len(list(auth_dir.glob("*.json"))) > 1:
        raise ValueError("single_proxy_account_required")


def write_status(config, state, running=True):
    value = {"running": running, "instance_id": state.instance_id, "observed_at": utcnow(),
             "local": state.local(), "tunnel_process_running": state.tunnel_running,
             "telemetry_ok": state.telemetry_ok, "quota_status": (state.quota or {}).get("status")}
    diag = getattr(state, "diagnostics", None)
    if diag: value["diagnostic_log"] = {"enabled": True, "write_error": diag.write_error, "version": 1}
    target = config.runtime_dir / "status.json"
    temp = target.with_suffix(".tmp")
    temp.write_text(json.dumps(value), encoding="utf-8")
    os.replace(temp, target)


async def run(config):
    config.runtime_dir.mkdir(parents=True, exist_ok=True)
    lock = InstanceLock(config.runtime_dir / "worker.lock")
    lock.acquire()
    state = State(config.worker_id)
    job = None
    commands, telemetry, quota, adapter_proc, ssh_proc = None, None, None, None, None
    tasks, servers = [], []
    diag = None
    stop_file = config.runtime_dir / "stop.request"
    try:
        job = Job()
        from .security import strict_acl
        if not strict_acl(config.runtime_dir.parent):
            raise ValueError("secret_root_acl_not_strict")
        diag = diagnostics(config, state)
        diag.emit("worker_started", process_id=os.getpid(), parent_process_id=os.getppid(), instance_id=state.instance_id)
        stop_file.unlink(missing_ok=True)
        if not port_available(8765) or (config.adapter.mode == "cliproxyapi" and not port_available(8317)):
            raise ValueError("required_port_in_use")
        if not config.enable_mock and config.adapter.mode == "cliproxyapi":
            check_proxy(config)
            adapter_proc = ManagedProcess([str(config.adapter.executable), "-config", str(config.adapter.config_file), "-local-model"],
                config.adapter.config_file.parent, job, env=quota_environment(config.quota.codex_home))
            await adapter_proc.tick()
        commands = Commands(config.runtime_dir / "commands.sqlite")
        quota = QuotaService(config, state, job)
        telemetry = Telemetry(config, state, commands)
        for logger_name in ("uvicorn.error", "uvicorn.access", "httpx", "httpcore"):
            logging.getLogger(logger_name).disabled = True
        if config.enable_mock:
            mock = uvicorn.Server(uvicorn.Config(create_mock(config), host="127.0.0.1", port=8317,
                log_config=None, access_log=False, proxy_headers=False, timeout_graceful_shutdown=5))
            servers.append(mock)
        app = create_app(config, state)
        bridge = uvicorn.Server(uvicorn.Config(app, host="127.0.0.1", port=8765, workers=1,
            log_config=None, access_log=False, proxy_headers=False, timeout_graceful_shutdown=5,
            h11_max_incomplete_event_size=65536, limit_concurrency=128))
        servers.append(bridge)
        for server in servers:
            # The supervisor owns Ctrl+C and local stop; no competing signal handlers.
            server.install_signal_handlers = lambda: None
            tasks.append(asyncio.create_task(server.serve(), name="bridge_server"))
        command = ssh_command(config)
        if command:
            if not config.ssh.identity_file.is_file() or not config.ssh.known_hosts_file.is_file():
                raise ValueError("ssh_trust_or_identity_missing")
            ssh_proc = ManagedProcess(command, config.runtime_dir, job, diagnostics=diag)

        async def control_loop():
            was_ready = False
            previous_auth_signature = None
            while True:
                if adapter_proc:
                    await adapter_proc.tick()
                if not config.enable_mock and config.adapter.mode == "cliproxyapi":
                    auth_files = sorted((config.adapter.config_file.parent / "proxy-auth").glob("*.json"))
                    if len(auth_files) > 1:
                        raise ValueError("single_proxy_account_required")
                    signature = hashlib.sha256(b"".join(hashlib.sha256(p.read_bytes()).digest() for p in auth_files)).hexdigest()
                    if previous_auth_signature is not None and previous_auth_signature != signature:
                        state.auth_state = "unknown"
                    previous_auth_signature = signature
                elif config.adapter.mode == "existing_codex_login":
                    from .identity import auth_signature
                    signature = auth_signature(config)
                    if previous_auth_signature is not None and previous_auth_signature != signature:
                        from .quota_normalizer import empty_quota
                        state.auth_state = "unknown"
                        quota.identity.reload()
                        state.quota = empty_quota(quota.identity.epoch, config.quota.account_alias, quota.identity.check)
                        quota.force_refresh = True
                    previous_auth_signature = signature
                ready = await app.state.adapter.probe()
                # Readiness is local initialization; the tunnel does not depend
                # on upstream network reachability.
                tunnel_ready = ready or (config.adapter.mode == "existing_codex_login"
                                         and state.adapter_status == "running")
                if ssh_proc and tunnel_ready and state.bridge_ready:
                    await ssh_proc.tick()
                    state.tunnel_running = ssh_proc.running
                elif ssh_proc:
                    await ssh_proc.stop()
                    state.tunnel_running = False
                if ready and not was_ready:
                    # Due flag is handled by the independent quota task.
                    quota.force_refresh = True
                was_ready = ready
                write_status(config, state)
                await asyncio.sleep(2)

        async def quota_loop():
            due, failures = 0.0, 0
            while True:
                await commands.execute(quota)
                if (time.monotonic() >= due or quota.force_refresh) and time.monotonic() - quota.last_refresh >= config.quota.minimum_refresh_seconds:
                    quota.force_refresh = False
                    ok = await quota.refresh()
                    diag.check("quota", ok, error_code="ok" if ok else "quota_unavailable")
                    failures = 0 if ok else failures + 1
                    interval = config.quota.poll_seconds if ok else min(900, config.quota.poll_seconds * 2 ** min(failures - 1, 3))
                    due = time.monotonic() + interval
                await asyncio.sleep(1)

        async def telemetry_loop():
            while True:
                await telemetry.send()
                await asyncio.sleep(config.telemetry.interval_seconds)

        tasks += [asyncio.create_task(control_loop(), name="control_loop"), asyncio.create_task(quota_loop(), name="quota_loop"), asyncio.create_task(telemetry_loop(), name="telemetry_loop")]
        while not stop_file.exists():
            if any(task.done() for task in tasks):
                for task in tasks:
                    if task.done():
                        cause = asyncio.CancelledError() if task.cancelled() else task.exception() or RuntimeError()
                        diag.emit("worker_component_failed", component=task.get_name(), **exception_info(cause))
                raise RuntimeError("worker_component_stopped")
            await asyncio.sleep(.2)
    except BaseException as exc:
        if diag: diag.emit("worker_failed", **exception_info(exc))
        raise
    finally:
        if diag: diag.emit("worker_stopping")
        for server in servers:
            server.should_exit = True
        server_tasks = tasks[:len(servers)]
        for task in tasks[len(servers):]:
            task.cancel()
        if tasks[len(servers):]:
            await asyncio.gather(*tasks[len(servers):], return_exceptions=True)
        if server_tasks:
            try:
                await asyncio.wait_for(asyncio.gather(*server_tasks, return_exceptions=True), 7)
            except TimeoutError:
                for t in server_tasks:
                    t.cancel()
                await asyncio.gather(*server_tasks, return_exceptions=True)
        if quota:
            await quota.close()
        if telemetry:
            await telemetry.close()
        for process in (ssh_proc, adapter_proc):
            if process:
                await process.stop()
        if commands:
            commands.close()
        if job:
            job.close()
        state.bridge_ready, state.tunnel_running, state.adapter_ready = False, False, False
        if diag: diag.emit("worker_stopped")
        write_status(config, state, running=False)
        stop_file.unlink(missing_ok=True)
        lock.release()
