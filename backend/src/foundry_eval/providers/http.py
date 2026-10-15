"""Single place that configures outbound HTTP (httpx2).

* TLS verification uses the operating system trust store (``truststore``), so corporate TLS
  inspection proxies trusted by Windows keep working.
* Redirects are never followed (a redirect could carry the API key to another host) and nothing
  is retried here.
* Loopback endpoints (local proxies, the mock server) never go through a configured HTTP proxy,
  which would otherwise receive the key in plain text.
"""

from __future__ import annotations

import ipaddress
import ssl
from functools import lru_cache
from urllib.parse import urlsplit

import httpx2
import truststore


@lru_cache(maxsize=1)
def ssl_context() -> ssl.SSLContext:
    return truststore.SSLContext(ssl.PROTOCOL_TLS_CLIENT)


def is_loopback_url(url: str) -> bool:
    host = (urlsplit(url).hostname or "").strip("[]").lower()
    if host == "localhost" or host.endswith(".localhost"):
        return True
    try:
        return ipaddress.ip_address(host).is_loopback
    except ValueError:
        return False


def make_timeout(connect: float, read: float, total: float | None = None) -> httpx2.Timeout:
    # httpx2 has no overall deadline; ``total`` is enforced by the caller with asyncio.timeout.
    del total
    return httpx2.Timeout(connect=connect, read=read, write=read, pool=connect)


def make_async_client(
    *,
    timeout: httpx2.Timeout,
    url: str,
    transport: httpx2.AsyncBaseTransport | None = None,
) -> httpx2.AsyncClient:
    """Client for one request to ``url``: no redirects, no proxy for loopback targets."""
    if transport is not None:
        return httpx2.AsyncClient(timeout=timeout, transport=transport, follow_redirects=False)
    return httpx2.AsyncClient(
        timeout=timeout,
        verify=ssl_context(),
        follow_redirects=False,
        trust_env=not is_loopback_url(url),
    )


def redirect_message(status: int, location: str | None) -> str:
    target = urlsplit(location or "")
    where = f" to {target.scheme}://{target.hostname}" if target.hostname else ""
    return (
        f"The endpoint answered with a redirect (HTTP {status}{where}). Redirects are not followed, so the API "
        "key is never sent to another address; check the resource URL."
    )
