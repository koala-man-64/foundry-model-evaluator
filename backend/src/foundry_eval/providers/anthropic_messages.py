"""Claude Messages API adapter for Microsoft Foundry, using the official Anthropic SDK.

* ``AsyncAnthropicFoundry`` sends the Foundry key headers and ``anthropic-version``.
* ``max_retries=0``: no automatic retries; every call is exactly one request.
* Streaming uses ``messages.stream()`` + ``get_final_message()``; the SDK refuses large
  non-streamed ``max_tokens`` unless an explicit timeout is configured (we always set one).
* SDK 1.x has no typed ``temperature``/``top_p``/``top_k`` parameters, so they are passed in
  ``extra_body`` for older models that still accept them.
* No refusal fallbacks: a refusal is recorded as such, never answered by another model.
"""

from __future__ import annotations

import asyncio
import time
from typing import Any

import anthropic
import httpx2
from anthropic import AsyncAnthropicFoundry, DefaultAsyncHttpxClient

from ..credentials.redaction import sanitize_error_text
from ..types import ApiStyle, FinishReason
from .base import ProviderCall, ProviderResult
from .http import is_loopback_url, make_timeout, redirect_message, ssl_context
from .openai_compat import classify_error

_TYPED_PARAMS = {
    "max_tokens",
    "messages",
    "model",
    "system",
    "thinking",
    "output_config",
    "stop_sequences",
    "metadata",
    "service_tier",
    "cache_control",
    "inference_geo",
}
_STOP_REASONS = {
    "end_turn": FinishReason.COMPLETED,
    "stop_sequence": FinishReason.COMPLETED,
    "max_tokens": FinishReason.MAX_TOKENS,
    "refusal": FinishReason.REFUSAL,
    "model_context_window_exceeded": FinishReason.CONTEXT_EXCEEDED,
}


def _ms(start: float) -> int:
    return int((time.perf_counter() - start) * 1000)


def parse_message(message: dict[str, Any]) -> dict[str, Any]:
    text: list[str] = []
    thinking: list[str] = []
    for block in message.get("content") or []:
        if not isinstance(block, dict):
            continue
        if block.get("type") == "text":
            text.append(str(block.get("text") or ""))
        elif block.get("type") == "thinking" and block.get("thinking"):
            thinking.append(str(block["thinking"]))
    stop = message.get("stop_reason")
    usage = message.get("usage") if isinstance(message.get("usage"), dict) else None
    return {
        "text": "".join(text),
        "summary": "\n\n".join(thinking) or None,
        "finish": _STOP_REASONS.get(str(stop), FinishReason.OTHER),
        "raw_finish": stop,
        "model": message.get("model"),
        "service_tier": usage.get("service_tier") if usage else None,
        "usage": usage,
    }


class AnthropicMessagesAdapter:
    api_style = ApiStyle.ANTHROPIC_MESSAGES

    def __init__(self, transport: httpx2.AsyncBaseTransport | None = None) -> None:
        self._transport = transport

    def _client(self, call: ProviderCall) -> AsyncAnthropicFoundry:
        # Never follow redirects: the SDK default would resend the key headers to the new host.
        if self._transport is not None:
            http_client = DefaultAsyncHttpxClient(transport=self._transport, follow_redirects=False)
        elif is_loopback_url(call.url):
            # An explicit transport stops the SDK from mounting environment proxies for this host.
            transport = httpx2.AsyncHTTPTransport(verify=ssl_context(), trust_env=False)
            http_client = DefaultAsyncHttpxClient(transport=transport, follow_redirects=False, trust_env=False)
        else:
            http_client = DefaultAsyncHttpxClient(verify=ssl_context(), follow_redirects=False)
        return AsyncAnthropicFoundry(
            api_key=call.api_key,
            base_url=call.url,
            max_retries=0,
            timeout=make_timeout(call.timeouts.connect, call.timeouts.read),
            http_client=http_client,
        )

    async def execute(self, call: ProviderCall) -> ProviderResult:
        body = dict(call.body)
        stream = bool(body.pop("stream", False))
        typed = {k: body.pop(k) for k in list(body) if k in _TYPED_PARAMS}
        extra = body or None
        client = self._client(call)
        start = time.perf_counter()
        ttft: int | None = None
        request_id: str | None = None
        try:
            async with asyncio.timeout(call.timeouts.total):
                if stream:
                    async with client.messages.stream(**typed, extra_body=extra) as message_stream:
                        request_id = message_stream.request_id
                        async for event in message_stream:
                            if (
                                ttft is None
                                and event.type == "content_block_delta"
                                and getattr(event.delta, "type", "") == "text_delta"
                            ):
                                ttft = _ms(start)
                        message = await message_stream.get_final_message()
                else:
                    raw = await client.messages.with_raw_response.create(**typed, extra_body=extra)
                    request_id = raw.headers.get("request-id") or raw.headers.get("apim-request-id")
                    message = await raw.parse()
        except anthropic.APIStatusError as exc:
            if 300 <= exc.status_code < 400:
                return self._error(
                    call,
                    start,
                    stream,
                    error_type="redirect",
                    message=redirect_message(exc.status_code, exc.response.headers.get("location")),
                    status=exc.status_code,
                    ttft=ttft,
                )
            error_type, finish, text = classify_error(exc.status_code, exc.body)
            return self._error(
                call,
                start,
                stream,
                error_type=error_type,
                message=f"HTTP {exc.status_code}: {text or exc.message}",
                status=exc.status_code,
                finish=finish,
                request_id=exc.response.headers.get("request-id") or exc.response.headers.get("apim-request-id"),
                retry_after=exc.response.headers.get("retry-after"),
                ttft=ttft,
            )
        except (anthropic.APITimeoutError, TimeoutError):
            return self._error(call, start, stream, error_type="timeout", message="Request timed out.", ttft=ttft)
        except anthropic.APIConnectionError as exc:
            return self._error(call, start, stream, error_type="network", message=f"Network error: {exc}", ttft=ttft)
        except httpx2.HTTPError as exc:  # e.g. the connection dropped mid-stream
            return self._error(call, start, stream, error_type="network", message=f"Network error: {exc}", ttft=ttft)
        except anthropic.APIError as exc:
            return self._error(
                call, start, stream, error_type="bad_response", message=f"Unexpected response: {exc}", ttft=ttft
            )
        finally:
            await client.close()

        data = message.model_dump(mode="json")
        parsed = parse_message(data)
        return ProviderResult(
            ok=True,
            request_body=call.body,
            streamed=stream,
            latency_ms=_ms(start),
            ttft_ms=ttft,
            response_json=data,
            raw_usage=parsed["usage"],
            output_text=parsed["text"],
            reasoning_summary=parsed["summary"],
            finish_reason=parsed["finish"],
            raw_finish_reason=parsed["raw_finish"],
            response_model=parsed["model"],
            service_tier=parsed["service_tier"],
            request_id=request_id,
            http_status=200,
        )

    def _error(
        self,
        call: ProviderCall,
        start: float,
        stream: bool,
        *,
        error_type: str,
        message: str,
        status: int | None = None,
        finish: FinishReason = FinishReason.ERROR,
        request_id: str | None = None,
        retry_after: str | None = None,
        ttft: int | None = None,
    ) -> ProviderResult:
        return ProviderResult(
            ok=False,
            request_body=call.body,
            streamed=stream,
            latency_ms=_ms(start),
            ttft_ms=ttft,
            http_status=status,
            request_id=request_id,
            finish_reason=finish,
            error_type=error_type,
            error_message=sanitize_error_text(message),
            retry_after=retry_after,
        )
