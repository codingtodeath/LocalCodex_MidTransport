import asyncio
import json
import sys
import time
import uuid
from pathlib import Path

import httpx
import pytest
from jsonschema import ValidationError
from pydantic import ValidationError as ConfigError

from windows.config import Config, read_key
from windows.contracts import validate
from windows.processes import InstanceLock, ManagedProcess
from windows.quota_client import QuotaClient, RPCError
from windows.quota_service import QuotaService
from windows.sse import SSEFramer
from windows.state import utcnow
from windows.telemetry import Commands, Telemetry
from windows.tunnel import ssh_command
from windows.winjob import Job


@pytest.mark.parametrize("field,value", [("protocol_version", "2"), ("inference_retries", 1), ("log_bodies", True)])
def test_config_fail_closed(config, field, value):
    data = config.model_dump()
    data[field] = value
    with pytest.raises(ConfigError):
        Config.model_validate(data)


def test_config_mock_cannot_leave_local(config):
    data = config.model_dump()
    data["telemetry"]["url"] = "https://example.invalid/internal/v1/telemetry"
    with pytest.raises(ConfigError):
        Config.model_validate(data)
    data["telemetry"]["url"] = None
    data["adapter"]["base_url"] = "http://evil:8317"
    with pytest.raises(ConfigError):
        Config.model_validate(data)


def test_single_instance(config):
    a = InstanceLock(config.runtime_dir / "worker.lock")
    b = InstanceLock(config.runtime_dir / "worker.lock")
    a.acquire()
    with pytest.raises(RuntimeError):
        b.acquire()
    a.release()
    b.acquire()
    b.release()


def test_ssh_strict_and_array(config):
    config.ssh.host = "server.example"
    command = ssh_command(config)
    assert "StrictHostKeyChecking=yes" in command and "ExitOnForwardFailure=yes" in command
    assert command[command.index("-R") + 1] == "127.0.0.1:18765:127.0.0.1:8765"
    assert "-N" in command and "-T" in command and "ForwardAgent=no" in command


async def test_stdio_rpc_ids_notifications_and_allowlist(config):
    notifications = []
    rpc = QuotaClient([sys.executable, str(Path(__file__).parent / "mock_app_server.py")],
                      config.quota.codex_home, timeout=1,
                      notification=lambda method, params: notifications.append(method))
    await rpc.start()
    try:
        account, limits = await asyncio.gather(rpc.request("account/read"), rpc.request("account/rateLimits/read"))
        assert account["account"]["type"] == "chatgpt" and limits["rateLimits"]["primary"]["usedPercent"] == 25
        assert notifications
        with pytest.raises(ValueError):
            await rpc.request("turn/start")
        with pytest.raises(RPCError) as exc:
            await rpc.request("account/usage/read")
        assert "CANARY" not in str(exc.value)
    finally:
        await rpc.close()
    assert rpc.process.returncode is not None and not rpc.pending


async def test_rpc_timeout_and_cleanup(config):
    rpc = QuotaClient([sys.executable, "-u", "-c", "import sys,time; sys.stdin.readline(); time.sleep(20)"],
                      config.quota.codex_home, timeout=.1)
    with pytest.raises(TimeoutError):
        await rpc.start()
    assert rpc.process.returncode is not None and not rpc.pending


def test_sse_byte_splits_multiline_crlf_and_error_redaction():
    raw = ('event: response.output_text.delta\r\ndata: {"type":\r\ndata: "response.output_text.delta","delta":"中文"}\r\n\r\n'
           'event: error\ndata: {"type":"error","message":"SYNTHETIC_CANARY","access_token":"SYNTHETIC_CANARY"}\n\n').encode()
    parser = SSEFramer()
    out = b"".join(frame for chunk in [raw[i:i + 1] for i in range(len(raw))] for frame in parser.feed(chunk))
    assert "中文".encode() in out and b"SYNTHETIC_CANARY" not in out
    assert not parser.buffer


def test_sse_duplicate_conflict_and_truncation():
    parser = SSEFramer()
    event = b'event: response.completed\ndata: {"type":"response.completed","response":{"id":"resp_x","status":"completed","usage":{"input_tokens":10}}}\n\n'
    parser.feed(event + event)
    assert parser.completed and not parser.conflict
    parser.feed(event.replace(b'"input_tokens":10', b'"input_tokens":11'))
    assert parser.conflict and not parser.completed
    truncated = SSEFramer()
    truncated.feed(b"data: {\"type\":\"response.output")
    assert truncated.buffer and not truncated.completed


async def test_telemetry_contract_ack_and_command_dedup(config, state):
    config.enable_mock = False
    config.telemetry.url = "https://server.example/internal/v1/telemetry"
    cmd_id = str(uuid.uuid4())
    sent = []
    def handler(request):
        obj = json.loads(request.content)
        validate("telemetry", obj)
        sent.append(obj)
        assert request.headers["authorization"] == "Bearer " + read_key(config.telemetry.secret_file, "tel_")
        return httpx.Response(200, json={"protocol_version": "1.0", "ack_seq": obj["seq"], "server_time": utcnow(),
            "commands": [{"id": cmd_id, "type": "refresh_quota", "expires_at": utcnow()}]})
    commands = Commands(config.runtime_dir / "commands.sqlite")
    tel = Telemetry(config, state, commands, httpx.MockTransport(handler))
    try:
        assert await tel.send()
        assert commands.results()[0]["status"] == "expired"
        assert await tel.send()
        assert not commands.results()
        assert sent[1]["seq"] == sent[0]["seq"] + 1
        assert sent[0]["instance_id"] == state.health()["instance_id"]
    finally:
        await tel.close()
        commands.close()


@pytest.mark.parametrize("variant", ["unknown_command", "extra_field", "wrong_ack", "superseded", "too_large", "redirect", "long_ttl"])
async def test_telemetry_rejects_untrusted_reply(config, state, variant):
    config.enable_mock = False
    config.telemetry.url = "https://server.example/internal/v1/telemetry"
    from datetime import datetime, timedelta, timezone
    def handler(r):
        obj = {"protocol_version": "1.0", "ack_seq": json.loads(r.content)["seq"], "server_time": utcnow(), "commands": []}
        headers = {}
        if variant == "unknown_command":
            obj["commands"] = [{"id": str(uuid.uuid4()), "type": "shell", "expires_at": utcnow()}]
        elif variant == "extra_field":
            obj["access_token"] = "SYNTHETIC_CANARY"
        elif variant == "wrong_ack":
            obj["ack_seq"] = 1234
        elif variant == "superseded":
            headers["X-Relay-Heartbeat"] = "heartbeat_from_superseded_instance"
        elif variant == "long_ttl":
            obj["commands"] = [{"id": str(uuid.uuid4()), "type": "refresh_quota",
                "expires_at": (datetime.now(timezone.utc) + timedelta(seconds=150)).isoformat()}]
        elif variant == "too_large":
            return httpx.Response(200, content=b"x" * 65537)
        elif variant == "redirect":
            return httpx.Response(302, headers={"Location": "https://evil"})
        return httpx.Response(200, json=obj, headers=headers)
    commands = Commands(config.runtime_dir / "commands.sqlite")
    tel = Telemetry(config, state, commands, httpx.MockTransport(handler))
    try:
        assert not await tel.send()
        assert not state.telemetry_ok and not commands.results()
    finally:
        await tel.close()
        commands.close()


async def test_command_persistent_dedup_crash_and_min_interval(config, state):
    from datetime import datetime, timezone, timedelta
    path = config.runtime_dir / "commands.sqlite"
    commands = Commands(path)
    ident = str(uuid.uuid4())
    command = {"id": ident, "type": "refresh_quota", "expires_at": (datetime.now(timezone.utc) + timedelta(seconds=60)).isoformat()}
    commands.accept(command)
    commands.accept(command)
    quota = QuotaService(config, state)
    quota.last_refresh = time.monotonic()
    await commands.execute(quota)
    assert commands.results() == [{"id": ident, "status": "accepted", "error_code": None}]
    quota.last_refresh -= 31
    await commands.execute(quota)
    assert commands.results()[0]["status"] == "done"
    commands.close()
    commands = Commands(path)
    commands.accept(command)
    assert len(commands.results()) == 1 and commands.results()[0]["status"] == "done"
    second = str(uuid.uuid4())
    commands.accept({**command, "id": second})
    commands.close()
    commands = Commands(path)
    assert next(x for x in commands.results() if x["id"] == second)["error_code"] == "worker_restarted"
    commands.close()


async def test_owned_process_cleanup_and_backoff(config):
    job = Job()
    proc = ManagedProcess([sys.executable, "-c", "import time; time.sleep(60)"], config.runtime_dir, job)
    await proc.tick()
    assert proc.running
    await proc.stop()
    assert not proc.running
    fail = ManagedProcess([sys.executable, "-c", "raise SystemExit(2)"], config.runtime_dir, job)
    await fail.tick()
    await asyncio.sleep(.2)
    await fail.tick()
    assert fail.failures == 1 and not fail.running
    assert fail.next_start > asyncio.get_running_loop().time()
    await fail.stop()
    job.close()
