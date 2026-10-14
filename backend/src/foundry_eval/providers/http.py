"""Single place that configures outbound HTTP (httpx2).

* TLS verification uses the operating system trust store (``truststore``), so corporate TLS
  inspection proxies trusted by Windows keep working.
* Redirects are never followed and nothing is retried here.
"""

from __future__ import annotations

import ssl
from functools import lru_cache

import httpx2
import truststore


@lru_cache(maxsize=1)
def ssl_context() -> ssl.SSLContext:
    return truststore.SSLContext(ssl.PROTOCOL_TLS_CLIENT)


def make_timeout(connect: float, read: float, total: float | None = None) -> httpx2.Timeout:
    # httpx2 has no overall deadline; ``total`` is enforced by the caller with asyncio.timeout.
    del total
    return httpx2.Timeout(connect=connect, read=read, write=read, pool=connect)


def make_async_client(
    *,
    timeout: httpx2.Timeout,
    transport: httpx2.AsyncBaseTransport | None = None,
) -> httpx2.AsyncClient:
    if transport is not None:
        return httpx2.AsyncClient(timeout=timeout, transport=transport, follow_redirects=False)
    return httpx2.AsyncClient(timeout=timeout, verify=ssl_context(), follow_redirects=False, trust_env=True)
