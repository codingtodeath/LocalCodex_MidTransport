"""Close an HTTP CONNECT tunnel if its TLS setup fails or is cancelled.

httpcore 1.0.7/1.0.9 leave the underlying CONNECT connection ACTIVE in
this case (encode/httpcore discussion 921). Repeated failures exhaust
the pool even though there are no live requests or sockets left.
"""
import anyio
import httpcore
import httpx
from httpcore._async.http_proxy import AsyncTunnelHTTPConnection


class SafeTunnelConnection(AsyncTunnelHTTPConnection):
    async def handle_async_request(self, request):
        try:
            return await super().handle_async_request(request)
        except BaseException:
            # Only an unfinished tunnel belongs to this cleanup. A connected
            # tunnel can carry other in-flight responses, especially with HTTP/2.
            if not self._connected:
                with anyio.move_on_after(4, shield=True):
                    await self.aclose()
            raise


class SafeProxyPool(httpcore.AsyncHTTPProxy):
    def create_connection(self, origin):
        if origin.scheme == b"http":
            return super().create_connection(origin)
        return SafeTunnelConnection(
            proxy_origin=self._proxy_url.origin,
            proxy_headers=self._proxy_headers,
            remote_origin=origin,
            ssl_context=self._ssl_context,
            proxy_ssl_context=self._proxy_ssl_context,
            keepalive_expiry=self._keepalive_expiry,
            http1=self._http1,
            http2=self._http2,
            network_backend=self._network_backend,
        )


class SafeProxyTransport(httpx.AsyncHTTPTransport):
    def __init__(self, proxy, *, limits=httpx.Limits(), verify=True):
        super().__init__(proxy=proxy, limits=limits, verify=verify, trust_env=False)
        pool = self._pool
        if not isinstance(pool, httpcore.AsyncHTTPProxy):
            raise ValueError("http_connect_proxy_required")
        # Retain HTTPX's TLS verification, proxy authentication and limits.
        # No global monkey patch and no closing another request's live stream.
        self._pool = SafeProxyPool(
            proxy_url=pool._proxy_url,
            proxy_headers=pool._proxy_headers,
            proxy_ssl_context=pool._proxy_ssl_context,
            ssl_context=pool._ssl_context,
            max_connections=pool._max_connections,
            max_keepalive_connections=pool._max_keepalive_connections,
            keepalive_expiry=pool._keepalive_expiry,
            http1=pool._http1,
            http2=pool._http2,
            network_backend=pool._network_backend,
        )
