import uuid
from datetime import datetime, timezone
from dataclasses import dataclass, field


def utcnow() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")


@dataclass
class State:
    worker_id: str
    instance_id: str = field(default_factory=lambda: str(uuid.uuid4()))
    bridge_ready: bool = False
    adapter_status: str = "stopped"
    adapter_ready: bool = False
    auth_state: str = "unknown"
    active_requests: int = 0
    last_inference_success_at: str | None = None
    quota: dict | None = None
    quota_supported: bool = False
    tunnel_running: bool = False
    telemetry_ok: bool = False

    def local(self):
        return {k: getattr(self, k) for k in (
            "bridge_ready", "adapter_status", "adapter_ready", "auth_state",
            "active_requests", "last_inference_success_at")}

    def health(self):
        return dict(protocol_version="1.0", worker_id=self.worker_id,
                    instance_id=self.instance_id, observed_at=utcnow(), local=self.local())
