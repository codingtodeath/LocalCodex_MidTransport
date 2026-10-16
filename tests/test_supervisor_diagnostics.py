import asyncio
import json
from types import SimpleNamespace

import pytest

from windows import supervisor
from windows.runtime_diagnostics import RuntimeDiagnostics


async def test_failed_background_component_is_identified_and_shutdown_cleans_up(config, monkeypatch):
    class Server:
        should_exit = False
        def __init__(self, config): pass
        async def serve(self):
            while not self.should_exit: await asyncio.sleep(.01)
    class Quota:
        force_refresh = False
        last_refresh = float('-inf')
        def __init__(self, config, state, job): pass
        async def refresh(self):
            await asyncio.sleep(.02)
            raise OSError('SECRET_BACKGROUND_FAILURE')
        async def close(self): pass
    class Telemetry:
        def __init__(self, *args): pass
        async def send(self): await asyncio.sleep(.05)
        async def close(self): pass
    async def probe(): return True
    monkeypatch.setattr(supervisor, 'Job', lambda: SimpleNamespace(close=lambda: None))
    monkeypatch.setattr('windows.security.strict_acl', lambda _: True)
    monkeypatch.setattr(supervisor, 'port_available', lambda _: True)
    monkeypatch.setattr(supervisor.uvicorn, 'Server', Server)
    monkeypatch.setattr(supervisor, 'QuotaService', Quota)
    monkeypatch.setattr(supervisor, 'Telemetry', Telemetry)
    monkeypatch.setattr(supervisor, 'create_app', lambda *a: SimpleNamespace(state=SimpleNamespace(adapter=SimpleNamespace(probe=probe))))
    monkeypatch.setattr(supervisor, 'ssh_command', lambda _: None)
    with pytest.raises(RuntimeError, match='worker_component_stopped'):
        await asyncio.wait_for(supervisor.run(config), 2)
    path=RuntimeDiagnostics(config).path
    events=[json.loads(line) for line in path.read_text().splitlines()]
    failure=next(e for e in events if e['event']=='worker_component_failed')
    assert failure['component']=='quota_loop' and failure['error_type']=='OSError' and failure['error_code']=='os_error'
    assert 'SECRET' not in path.read_text()
    assert events[-1]['event']=='worker_stopped'
    assert not json.loads((config.runtime_dir/'status.json').read_text())['running']
