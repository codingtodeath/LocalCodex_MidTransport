"""Read local generated keys in memory, test the running dev worker; print metadata only."""
import argparse
import asyncio
import json
import time

import httpx

from windows.config import load_config, read_key
from windows.contracts import validate


async def smoke(config):
    if not config.enable_mock:
        raise RuntimeError("mock_only")
    async with httpx.AsyncClient(base_url="http://127.0.0.1:8765", trust_env=False,
        headers={"Authorization": "Bearer " + read_key(config.bridge.auth_secret_file, "wkr_")}, timeout=10) as client:
        health = (await client.get("/internal/v1/health")).json()
        validate("health", health)
        assert health["local"]["bridge_ready"] and health["local"]["adapter_ready"]
        models = (await client.get("/v1/models")).json()
        assert [x["id"] for x in models["data"]] == ["mock-codex"]
        start, first_byte, events = time.monotonic(), None, 0
        async with client.stream("POST", "/v1/responses", json={"model": "mock-codex", "stream": True, "input": "合成中文测试"}) as response:
            assert response.status_code == 200
            async for chunk in response.aiter_bytes():
                if first_byte is None:
                    first_byte = time.monotonic() - start
                events += chunk.count(b"event:")
        total = time.monotonic() - start
        assert first_byte < total / 2
        results = []
        for i in range(100):
            response = await client.post("/v1/responses", json={"model": "mock-codex", "stream": i % 2 == 0, "input": "合成测试"})
            results.append(response.status_code)
        assert all(code == 200 for code in results)
        denied = await client.post("/v1/responses", json={"model": "not-allowed"})
        assert denied.status_code == 403
        for path in ("/v0/management", "/v8/management", "/app-server", "/auth-files"):
            assert (await client.get(path)).status_code == 404
        health_after = (await client.get("/internal/v1/health")).json()
        assert health_after["local"]["active_requests"] == 0
        assert health_after["instance_id"] == health["instance_id"]
        return {"mode": "mock", "mixed_requests": len(results), "statuses_ok": True,
                "first_byte_ms": round(first_byte * 1000, 1), "total_ms": round(total * 1000, 1),
                "streamed_before_completion": True, "terminal_events_seen": events,
                "active_requests_after": 0, "management_blocked": True}


if __name__ == "__main__":
    p = argparse.ArgumentParser()
    p.add_argument("--config", required=True)
    args = p.parse_args()
    print(json.dumps(asyncio.run(smoke(load_config(args.config))), indent=2))
