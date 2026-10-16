import asyncio
import json
import httpx

from .config import Config, read_key
from .routing import routing_headers


class ProxyAdapter:
    def __init__(self, config: Config, state, transport=None):
        self.config, self.state = config, state
        self.key = read_key(config.adapter.api_key_file, "cpa_")
        timeout = httpx.Timeout(connect=config.bridge.connect_seconds,
                                read=config.bridge.idle_seconds, write=30, pool=5)
        common = dict(base_url=config.adapter.base_url, trust_env=False,
                      follow_redirects=False, transport=transport)
        self.models = httpx.AsyncClient(**common, timeout=timeout,
                                       limits=httpx.Limits(max_connections=config.bridge.max_concurrency + 2))
        self.control = httpx.AsyncClient(**common, timeout=2,
                                        limits=httpx.Limits(max_connections=2))

    def headers(self):
        return {"Authorization": f"Bearer {self.key}", "Accept-Encoding": "identity",
                "Content-Type": "application/json"}

    async def probe(self):
        try:
            r = await self.control.get("/v1/models", headers=self.headers())
            self.state.adapter_ready = r.status_code == 200 and isinstance(r.json().get("data"), list)
            self.state.adapter_status = "running" if r.status_code < 500 else "error"
            # Models is a local catalog; it does not prove upstream subscription auth.
        except (httpx.HTTPError, ValueError, AttributeError):
            self.state.adapter_ready = False
            self.state.adapter_status = "error"
        return self.state.adapter_ready

    async def open(self, method, path, body=None, request_headers=None):
        headers = self.headers()
        headers.update(routing_headers(request_headers))
        request = self.models.build_request(method, path, headers=headers, content=body)
        return await self.models.send(request, stream=True)

    async def close(self):
        await asyncio.gather(self.models.aclose(), self.control.aclose())
