"""Provider execution interface shared by all adapters."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Protocol

from ..types import ApiStyle, FinishReason


@dataclass(frozen=True)
class Prompt:
    user: str
    system: str = ""


@dataclass(frozen=True)
class Timeouts:
    connect: float = 15.0
    read: float = 600.0
    total: float = 900.0


@dataclass(frozen=True)
class ProviderCall:
    api_style: ApiStyle
    url: str  # full request URL (OpenAI-compatible) or SDK base URL (Claude)
    query: dict[str, str]
    auth_style: str
    api_key: str = field(repr=False)
    body: dict[str, Any] = field(default_factory=dict)  # exact provider request body (no secrets)
    timeouts: Timeouts = field(default_factory=Timeouts)

    @property
    def stream(self) -> bool:
        return bool(self.body.get("stream"))


@dataclass
class ProviderResult:
    ok: bool
    request_body: dict[str, Any]
    streamed: bool
    latency_ms: int
    response_json: dict[str, Any] | None = None
    raw_usage: dict[str, Any] | None = None
    output_text: str = ""
    reasoning_summary: str | None = None
    finish_reason: FinishReason = FinishReason.ERROR
    raw_finish_reason: str | None = None
    response_model: str | None = None
    service_tier: str | None = None
    request_id: str | None = None
    http_status: int | None = None
    ttft_ms: int | None = None
    error_type: str | None = None
    error_message: str | None = None
    retry_after: str | None = None


def unusable_answer(result: ProviderResult) -> str | None:
    """Why a response the provider completed is still not an answer to compare, or None if it is one.

    Such a response was billed, so it keeps its usage and cost, but it must not enter comparisons or
    the judge as if it were an (empty) answer.
    """
    if not result.ok:
        return None
    if result.finish_reason is FinishReason.TOOL_CALL:
        return "The model asked to call a tool instead of answering; tool calls are not supported."
    if not result.output_text.strip():
        return f"The model returned no answer text (finish reason: {result.finish_reason.value})."
    return None


class ProviderAdapter(Protocol):
    api_style: ApiStyle

    async def execute(self, call: ProviderCall) -> ProviderResult: ...
