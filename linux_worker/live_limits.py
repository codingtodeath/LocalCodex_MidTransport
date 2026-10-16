"""Root-owned, non-secret limits shared by Gateway and Worker."""
import json
from dataclasses import replace
from pathlib import Path

LIMITS_FILE = Path('/opt/codex-relay-worker/runtime-limits.json')


def read_limits(path=LIMITS_FILE):
    try:
        data = json.loads(path.read_text())
        global_limit, per_client = data['global_limit'], data['per_client_limit']
        if type(global_limit) is not int or type(per_client) is not int:
            return None
        if not 1 <= per_client <= global_limit <= 100:
            return None
        return global_limit, per_client
    except (OSError, ValueError, KeyError, TypeError):
        return None


def apply_gateway_limits(services, path=LIMITS_FILE):
    values = read_limits(path)
    if values is None:
        return
    with services.gate._lock:
        services.gate.global_limit, services.gate.per_client_limit = values
        if (services.config.max_concurrency, services.config.per_client_concurrency) != values:
            services.config = replace(services.config, max_concurrency=values[0], per_client_concurrency=values[1])


def apply_worker_limits(config, path=LIMITS_FILE):
    values = read_limits(path)
    if values is not None:
        config.bridge.max_concurrency = values[0]
