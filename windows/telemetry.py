import asyncio
import json
import sqlite3
import ssl
import time
from datetime import datetime, timezone

import httpx

from .bridge import bounded_read
from .config import read_key
from .contracts import validate
from .state import utcnow


def timestamp(value):
    return datetime.fromisoformat(value.replace("Z", "+00:00")).timestamp()


class Commands:
    """Durable dedup and result outbox. Contains only schema-limited metadata."""
    def __init__(self, path):
        path.parent.mkdir(parents=True, exist_ok=True)
        self.db = sqlite3.connect(path)
        self.db.execute("CREATE TABLE IF NOT EXISTS commands (id TEXT PRIMARY KEY, expires REAL, status TEXT, error TEXT, dirty INTEGER)")
        # Crash before completion must not execute the accepted command twice.
        self.db.execute("UPDATE commands SET status='failed', error='worker_restarted', dirty=1 WHERE status='accepted'")
        self.db.commit()

    def accept(self, command):
        if command.get("type") != "refresh_quota":
            return False
        expiry = timestamp(command["expires_at"])
        status = "accepted" if expiry > time.time() else "expired"
        self.db.execute("INSERT OR IGNORE INTO commands VALUES (?,?,?,?,1)", (command["id"], expiry, status, None))
        self.db.commit()
        return True

    def results(self):
        return [{"id": i, "status": status, "error_code": error} for i, status, error in
                self.db.execute("SELECT id,status,error FROM commands WHERE dirty=1 ORDER BY expires LIMIT 16")]

    def ack(self, rows):
        for r in rows:
            self.db.execute("UPDATE commands SET dirty=0 WHERE id=? AND status=?", (r["id"], r["status"]))
        self.db.execute("DELETE FROM commands WHERE expires < ? AND dirty=0", (time.time() - 30 * 86400,))
        self.db.commit()

    async def execute(self, quota):
        rows = list(self.db.execute("SELECT id,expires FROM commands WHERE status='accepted' ORDER BY expires"))
        for ident, expiry in rows:
            if expiry <= time.time():
                self.finish(ident, "expired")
            elif time.monotonic() - quota.last_refresh >= quota.config.quota.minimum_refresh_seconds:
                ok = await quota.refresh()
                self.finish(ident, "done" if ok else "failed", None if ok else "quota_refresh_failed")

    def finish(self, ident, status, code=None):
        self.db.execute("UPDATE commands SET status=?,error=?,dirty=1 WHERE id=?", (status, code, ident))
        self.db.commit()

    def close(self):
        self.db.close()


class Telemetry:
    def __init__(self, config, state, commands, transport=None):
        self.config, self.state, self.commands = config, state, commands
        from .runtime_diagnostics import diagnostics
        self.diagnostics = diagnostics(config, state)
        self.seq = 0
        self.key = read_key(config.telemetry.secret_file, "tel_") if config.telemetry.url else None
        context = ssl.create_default_context(cafile=str(config.telemetry.ca_cert_file)) if config.telemetry.ca_cert_file else True
        self.client = httpx.AsyncClient(verify=context, trust_env=False, follow_redirects=False,
                                       timeout=5, transport=transport,
                                       limits=httpx.Limits(max_connections=2))

    def snapshot(self):
        self.seq += 1
        return validate("telemetry", dict(protocol_version="1.0", worker_id=self.config.worker_id,
            instance_id=self.state.instance_id, seq=self.seq, sent_at=utcnow(),
            capabilities=dict(responses_http=True, responses_nonstream=True,
                              quota_read=self.state.quota_supported, account_usage_read=False),
            local=self.state.local(), quota=self.state.quota,
            account_usage=None, command_results=self.commands.results()))

    async def send(self):
        data = self.snapshot()
        if not self.config.telemetry.url:
            return False
        if self.config.environment == "production" and (data.get("quota") or {}).get("source") == "mock":
            return False
        body = json.dumps(data, ensure_ascii=False, allow_nan=False).encode()
        if len(body) > 65536:
            self.state.telemetry_ok = False
            self.diagnostics.check("telemetry", False, error_code="body_too_large", seq=data["seq"])
            return False
        from .runtime_diagnostics import exception_info, http_code
        started = time.monotonic()
        details = {"seq": data["seq"], "error_code": "invalid_response"}
        try:
            async with self.client.stream("POST", self.config.telemetry.url, content=body,
                headers={"Authorization": f"Bearer {self.key}", "Content-Type": "application/json", "Accept-Encoding": "identity"}) as r:
                details["http_status"] = r.status_code
                if r.status_code != 200:
                    details["error_code"] = http_code(r.status_code)
                    raise ValueError("telemetry_rejected")
                reply = json.loads(await bounded_read(r, 65536))
                heartbeat = r.headers.get("X-Relay-Heartbeat", "")
                if heartbeat in ("stale_seq", "heartbeat_from_superseded_instance"):
                    details["error_code"] = "heartbeat_not_current"
                    raise ValueError("heartbeat_not_current")
            validate("telemetry-reply", reply)
            if reply["ack_seq"] != data["seq"] or abs(timestamp(reply["server_time"]) - time.time()) > 120:
                details["error_code"] = "invalid_ack"
                raise ValueError("telemetry_invalid_ack_or_clock")
            for command in reply["commands"]:
                if timestamp(command["expires_at"]) - timestamp(reply["server_time"]) > 60:
                    details["error_code"] = "invalid_command"
                    raise ValueError("command_ttl_too_long")
            self.commands.ack(data["command_results"])
            for command in reply["commands"]:
                self.commands.accept(command)
            self.state.telemetry_ok = True
            self.diagnostics.check("telemetry", True, seq=data["seq"], http_status=200,
                duration_ms=round((time.monotonic()-started)*1000))
            return True
        except (httpx.HTTPError, ValueError, TypeError, KeyError) as exc:
            safe = exception_info(exc)
            if isinstance(exc, httpx.HTTPError): details["error_code"] = safe["error_code"]
            self.diagnostics.check("telemetry", False, **details, error_type=safe["error_type"],
                duration_ms=round((time.monotonic()-started)*1000))
            self.state.telemetry_ok = False
            return False
        except Exception as exc:
            # jsonschema errors can embed the rejected object; never log exception text.
            from jsonschema import ValidationError
            if isinstance(exc, ValidationError):
                self.state.telemetry_ok = False
                self.diagnostics.check("telemetry", False, **details, error_type="ValidationError",
                    duration_ms=round((time.monotonic()-started)*1000))
                return False
            raise

    async def close(self):
        await self.client.aclose()
