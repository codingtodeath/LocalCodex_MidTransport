"""Exercise real pool exhaustion after failed/cancelled proxy TLS setup.

All traffic stays on loopback. No OpenAI requests, VPN use or TLS bypass.
"""
import asyncio
from contextlib import asynccontextmanager

import httpx
import pytest

from windows.proxy_transport import SafeProxyTransport


@asynccontextmanager
async def failing_proxy(mode="bad_tls"):
    started = asyncio.Event()
    release = asyncio.Event()
    handlers = set()

    async def handle(reader, writer):
        handlers.add(asyncio.current_task())
        try:
            head = await reader.readuntil(b"\r\n\r\n")
            if head.startswith(b"CONNECT "):
                writer.write(b"HTTP/1.1 200 Connection Established\r\n\r\n")
                await writer.drain()
                await reader.read(4096)  # ClientHello
                started.set()
                if mode == "bad_tls":
                    writer.write(b"HTTP/1.1 502 Bad Gateway\r\n\r\n")
                    await writer.drain()
                else:
                    await reader.read()  # TLS handshake stalls until cancellation.
            elif b"/hold " in head:
                writer.write(b"HTTP/1.1 200 OK\r\nTransfer-Encoding: chunked\r\n\r\n3\r\none\r\n")
                await writer.drain()
                await release.wait()
                writer.write(b"3\r\ntwo\r\n0\r\n\r\n")
                await writer.drain()
            else:
                writer.write(b"HTTP/1.1 200 OK\r\nContent-Length: 2\r\nConnection: close\r\n\r\nOK")
                await writer.drain()
        except (OSError, asyncio.IncompleteReadError):
            pass
        finally:
            writer.close()
            try:
                await writer.wait_closed()
            except OSError:
                pass
            handlers.discard(asyncio.current_task())

    server = await asyncio.start_server(handle, "127.0.0.1", 0)
    try:
        yield f"http://127.0.0.1:{server.sockets[0].getsockname()[1]}", started, release
    finally:
        release.set()
        server.close()
        await server.wait_closed()
        pending = list(handlers)
        for task in pending:
            task.cancel()
        await asyncio.gather(*pending, return_exceptions=True)


@pytest.mark.parametrize("fixed", [False, True])
async def test_repeated_tls_failure_does_not_exhaust_pool(fixed):
    async with failing_proxy() as (proxy, _, release):
        cls = SafeProxyTransport if fixed else httpx.AsyncHTTPTransport
        transport = cls(proxy=proxy, limits=httpx.Limits(max_connections=2))
        async with httpx.AsyncClient(transport=transport, timeout=.4) as client:
            # Keep an unrelated response open throughout the failure sequence.
            live = await client.send(client.build_request("GET", "http://example.test/hold"), stream=True)
            types = []
            for _ in range(5):
                try:
                    await client.get("https://example.test/fail")
                except httpx.HTTPError as exc:
                    types.append(type(exc))
            if not fixed:
                # Reproduce the original defect: only one free slot is leaked.
                assert types[0] is httpx.ConnectError
                assert types[1:] == [httpx.PoolTimeout] * 4
            else:
                assert types == [httpx.ConnectError] * 5
                assert (await client.get("http://example.test/ok")).text == "OK"
            release.set()
            assert await live.aread() == b"onetwo"
            await live.aclose()


async def test_cancelled_tls_handshake_releases_pool_slot():
    async with failing_proxy("stall") as (proxy, started, _):
        transport = SafeProxyTransport(proxy, limits=httpx.Limits(max_connections=1))
        async with httpx.AsyncClient(transport=transport, timeout=httpx.Timeout(2, pool=.3)) as client:
            for _ in range(3):
                started.clear()
                pending = asyncio.create_task(client.get("https://example.test/fail"))
                await asyncio.wait_for(started.wait(), 2)
                pending.cancel()
                with pytest.raises(asyncio.CancelledError):
                    await pending
            assert (await client.get("http://example.test/ok")).status_code == 200


async def test_timed_out_tls_handshake_releases_pool_slot():
    async with failing_proxy("stall") as (proxy, _, _):
        transport = SafeProxyTransport(proxy, limits=httpx.Limits(max_connections=1))
        async with httpx.AsyncClient(transport=transport, timeout=.15) as client:
            for _ in range(3):
                with pytest.raises(httpx.ConnectTimeout):
                    await client.get("https://example.test/fail")
            assert (await client.get("http://example.test/ok")).status_code == 200
