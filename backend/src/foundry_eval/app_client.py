"""A thin client of the running app's local API, for headless commands.

Offline commands take the instance lock and cannot run next to the app, so headless commands talk to
the running app instead. Every request carries what the web interface sends: the app's own ``Origin``
and ``X-FME-Client: 1``, and paid requests an ``Idempotency-Key``.

Nothing is ever retried. The transport has no retries, and a paid request that fails or times out is
reported, not repeated: repeating the command with the same key returns the first result without
paying twice.
"""

from __future__ import annotations

from types import TracebackType
from typing import Any

import httpx2

DEFAULT_TIMEOUT = 60.0


class AppUnavailableError(Exception):
    """The app did not accept the connection, so the request was never delivered."""


class RequestOutcomeUnknownError(Exception):
    """The request may have reached the app, but no answer came back (for example a timeout)."""


class ApiError(Exception):
    def __init__(self, status: int, detail: str) -> None:
        super().__init__(f"{detail} (HTTP {status})")
        self.status = status
        self.detail = detail


def _detail(response: httpx2.Response) -> str:
    try:
        body = response.json()
    except ValueError:
        return response.text[:500] or response.reason_phrase
    detail = body.get("detail") if isinstance(body, dict) else None
    if isinstance(detail, list):  # request validation errors
        return "; ".join(str(item.get("msg", item)) if isinstance(item, dict) else str(item) for item in detail)
    return str(detail) if detail is not None else response.text[:500]


class AppClient:
    def __init__(self, origin: str, *, http: httpx2.Client | None = None, timeout: float = DEFAULT_TIMEOUT) -> None:
        self.origin = origin
        self._owns_http = http is None
        # No proxies from the environment: the app is on loopback and must be reached directly.
        self._http = http or httpx2.Client(
            base_url=origin, transport=httpx2.HTTPTransport(retries=0), timeout=timeout, trust_env=False
        )
        self._headers = {"Origin": origin, "X-FME-Client": "1", "Accept": "application/json"}

    def __enter__(self) -> AppClient:
        return self

    def __exit__(
        self, exc_type: type[BaseException] | None, exc: BaseException | None, tb: TracebackType | None
    ) -> None:
        self.close()

    def close(self) -> None:
        if self._owns_http:
            self._http.close()

    def get(self, path: str, params: list[tuple[str, str]] | dict[str, str] | None = None) -> Any:
        return self._send("GET", path, params=params)

    def post(self, path: str, body: dict[str, Any], *, idempotency_key: str | None = None) -> Any:
        headers = {"Idempotency-Key": idempotency_key} if idempotency_key else {}
        return self._send("POST", path, json=body, headers=headers)

    def _send(
        self,
        method: str,
        path: str,
        *,
        params: list[tuple[str, str]] | dict[str, str] | None = None,
        json: dict[str, Any] | None = None,
        headers: dict[str, str] | None = None,
    ) -> Any:
        try:
            response = self._http.request(
                method,
                path,
                params=tuple(params.items() if isinstance(params, dict) else params or ()),
                json=json,
                headers={**self._headers, **(headers or {})},
            )
        except httpx2.ConnectError as exc:
            raise AppUnavailableError(
                f"The app is not running at {self.origin}. Start it (scripts\\start.cmd or foundry-eval serve) "
                "and try again."
            ) from exc
        except httpx2.TransportError as exc:
            raise RequestOutcomeUnknownError(f"No answer from the app at {self.origin}: {exc}") from exc
        if response.status_code >= 400:
            raise ApiError(response.status_code, _detail(response))
        return response.json()
