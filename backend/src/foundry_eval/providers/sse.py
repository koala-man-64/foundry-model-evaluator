"""Minimal server-sent events parser for streaming provider responses."""

from __future__ import annotations

from collections.abc import AsyncIterator
from dataclasses import dataclass

import httpx2


@dataclass(frozen=True)
class SSEEvent:
    event: str | None
    data: str


async def iter_sse(response: httpx2.Response) -> AsyncIterator[SSEEvent]:
    event: str | None = None
    data: list[str] = []
    async for line in response.aiter_lines():
        if line == "":
            if data:
                yield SSEEvent(event, "\n".join(data))
            event, data = None, []
            continue
        if line.startswith(":"):
            continue
        name, _, value = line.partition(":")
        if value.startswith(" "):
            value = value[1:]
        if name == "event":
            event = value
        elif name == "data":
            data.append(value)
    if data:
        yield SSEEvent(event, "\n".join(data))
