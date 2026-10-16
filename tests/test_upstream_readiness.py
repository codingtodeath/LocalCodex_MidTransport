import asyncio

import httpx

from windows.bridge import create_app
from windows.existing_adapter import ExistingAdapter
from windows.telemetry import Commands, Telemetry
from tests.test_single_login import single, session
from tests.test_runtime_diagnostics import write_auth, rows


async def test_local_health_and_lifespan_never_poll_openai(config, state, tmp_path):
    auth = single(config, tmp_path)
    original = auth.read_bytes()
    calls = []
    def handler(request):
        calls.append(request)
        raise AssertionError('health must not call OpenAI')
    adapter = ExistingAdapter(config, state, transport=httpx.MockTransport(handler))
    app = create_app(config, state, adapter)
    commands = Commands(config.runtime_dir / 'commands.sqlite')
    telemetry = Telemetry(config, state, commands)
    try:
        async with app.router.lifespan_context(app):
            async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url='http://bridge',
                    headers={'Authorization': 'Bearer ' + config.bridge.auth_secret_file.read_text()}) as client:
                for _ in range(4):
                    assert await adapter.probe()
                    health = (await client.get('/internal/v1/health')).json()
                    assert health['local']['adapter_ready']
                    assert health['local']['auth_state'] == 'unknown'
                    assert telemetry.snapshot()['local']['adapter_ready']
                await asyncio.sleep(.03)
        assert not state.bridge_ready and adapter.control.is_closed
        assert calls == [] and auth.read_bytes() == original
        assert not any(r['event'].startswith('openai_check') for r in rows(adapter.diagnostics))
    finally:
        await telemetry.close(); commands.close(); await adapter.close()


async def test_unauthorized_stays_reauth_until_token_changes(config, state, tmp_path):
    auth = single(config, tmp_path)
    calls = []
    def handler(request):
        calls.append(request)
        return httpx.Response(401)
    adapter = ExistingAdapter(config, state, transport=httpx.MockTransport(handler))
    try:
        assert await adapter.probe()
        r = await adapter.open('POST', '/v1/responses', b'{"model":"mock-codex","stream":true}')
        await r.aclose()
        assert not await adapter.probe() and state.auth_state == 'reauth_required'
        session(auth, suffix='rotated')
        assert await adapter.probe() and state.auth_state == 'unknown'
        assert len(calls) == 1
    finally:
        await adapter.close()


async def test_expired_and_missing_credentials_fail_locally(config, state, tmp_path):
    auth = single(config, tmp_path)
    def handler(request):
        raise AssertionError('no credential check should call OpenAI')
    adapter = ExistingAdapter(config, state, transport=httpx.MockTransport(handler))
    try:
        write_auth(auth, hours=-1)
        assert not await adapter.probe() and state.auth_state == 'reauth_required'
        write_auth(auth, hours=1)
        assert await adapter.probe()
        auth.unlink()
        assert not await adapter.probe() and state.adapter_status == 'error'
    finally:
        await adapter.close()
