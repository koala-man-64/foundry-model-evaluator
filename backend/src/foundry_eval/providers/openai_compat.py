"""OpenAI-compatible adapters (Responses API and Chat Completions) over raw httpx2.

No automatic retries: every call is exactly one HTTP request.
"""

from __future__ import annotations

import asyncio
import json
import time
from typing import Any

import httpx2

from ..credentials.redaction import sanitize_error_text
from ..types import ApiStyle, FinishReason
from .base import ProviderCall, ProviderResult
from .http import make_async_client, make_timeout
from .sse import iter_sse

_REQUEST_ID_HEADERS = ("x-request-id", "apim-request-id", "request-id", "x-ms-request-id")


def _ms(start: float, end: float | None = None) -> int:
    return int(((end if end is not None else time.perf_counter()) - start) * 1000)


def _request_id(headers: httpx2.Headers) -> str | None:
    for name in _REQUEST_ID_HEADERS:
        if name in headers:
            return headers[name]
    return None


def _auth_headers(call: ProviderCall) -> dict[str, str]:
    if call.auth_style == "bearer":
        return {"Authorization": f"Bearer {call.api_key}"}
    return {"api-key": call.api_key}


def classify_error(status: int | None, payload: Any) -> tuple[str, FinishReason, str]:
    """Map an error response to (error_type, finish_reason, message)."""
    message = ""
    code = ""
    if isinstance(payload, dict):
        err = payload.get("error", payload)
        if isinstance(err, dict):
            message = str(err.get("message") or "")
            code = str(err.get("code") or err.get("type") or "")
        elif isinstance(err, str):
            message = err
    lowered = f"{code} {message}".lower()
    if "content_filter" in lowered or "content management policy" in lowered:
        return "content_filter", FinishReason.CONTENT_FILTER, message or "Request blocked by content filtering."
    if "context_length" in lowered or "maximum context length" in lowered or "too long" in lowered:
        return "context_exceeded", FinishReason.CONTEXT_EXCEEDED, message or "Input exceeds the model's context window."
    if status == 429:
        return "rate_limited", FinishReason.ERROR, message or "Rate limited by the provider."
    if status in (401, 403):
        return "auth", FinishReason.ERROR, message or "Authentication failed; check the endpoint's API key."
    if status is not None and status >= 500:
        return "provider_error", FinishReason.ERROR, message or f"Provider returned HTTP {status}."
    return "bad_request" if status and status < 500 else "error", FinishReason.ERROR, message or f"HTTP {status}"


def _error_result(
    call: ProviderCall,
    start: float,
    *,
    error_type: str,
    message: str,
    status: int | None = None,
    finish: FinishReason = FinishReason.ERROR,
    request_id: str | None = None,
    retry_after: str | None = None,
    ttft_ms: int | None = None,
    response_json: dict[str, Any] | None = None,
) -> ProviderResult:
    return ProviderResult(
        ok=False,
        request_body=call.body,
        streamed=call.stream,
        latency_ms=_ms(start),
        ttft_ms=ttft_ms,
        http_status=status,
        request_id=request_id,
        finish_reason=finish,
        error_type=error_type,
        error_message=sanitize_error_text(message),
        retry_after=retry_after,
        response_json=response_json,
    )


def parse_responses_object(obj: dict[str, Any]) -> dict[str, Any]:
    text_parts: list[str] = []
    summaries: list[str] = []
    refusal = False
    for item in obj.get("output") or []:
        if not isinstance(item, dict):
            continue
        if item.get("type") == "message":
            for part in item.get("content") or []:
                if isinstance(part, dict) and part.get("type") == "output_text":
                    text_parts.append(str(part.get("text") or ""))
                elif isinstance(part, dict) and part.get("type") == "refusal":
                    refusal = True
                    text_parts.append(str(part.get("refusal") or ""))
        elif item.get("type") == "reasoning":
            for summary in item.get("summary") or []:
                if isinstance(summary, dict) and summary.get("text"):
                    summaries.append(str(summary["text"]))
    status = obj.get("status")
    details = obj.get("incomplete_details") or {}
    reason = details.get("reason") if isinstance(details, dict) else None
    if refusal:
        finish = FinishReason.REFUSAL
    elif status == "completed":
        finish = FinishReason.COMPLETED
    elif status == "incomplete" and reason == "max_output_tokens":
        finish = FinishReason.MAX_TOKENS
    elif status == "incomplete" and reason == "content_filter":
        finish = FinishReason.CONTENT_FILTER
    elif status == "failed":
        finish = FinishReason.ERROR
    else:
        finish = FinishReason.OTHER
    return {
        "text": "".join(text_parts),
        "summary": "\n\n".join(summaries) or None,
        "finish": finish,
        "raw_finish": f"{status}:{reason}" if reason else status,
        "model": obj.get("model"),
        "service_tier": obj.get("service_tier"),
        "usage": obj.get("usage") if isinstance(obj.get("usage"), dict) else None,
    }


_CHAT_FINISH = {
    "stop": FinishReason.COMPLETED,
    "length": FinishReason.MAX_TOKENS,
    "content_filter": FinishReason.CONTENT_FILTER,
}


def parse_chat_object(obj: dict[str, Any]) -> dict[str, Any]:
    choices = obj.get("choices") or []
    first = choices[0] if choices and isinstance(choices[0], dict) else {}
    message = first.get("message") or {}
    content = message.get("content")
    if isinstance(content, list):
        content = "".join(str(p.get("text") or "") for p in content if isinstance(p, dict))
    refusal = message.get("refusal")
    raw_finish = first.get("finish_reason")
    finish = FinishReason.REFUSAL if refusal else _CHAT_FINISH.get(str(raw_finish), FinishReason.OTHER)
    return {
        "text": str(content or refusal or ""),
        "summary": message.get("reasoning_content") or None,
        "finish": finish,
        "raw_finish": raw_finish,
        "model": obj.get("model"),
        "service_tier": obj.get("service_tier"),
        "usage": obj.get("usage") if isinstance(obj.get("usage"), dict) else None,
    }


class _OpenAICompatibleAdapter:
    api_style: ApiStyle

    def __init__(self, transport: httpx2.AsyncBaseTransport | None = None) -> None:
        self._transport = transport

    def _parse(self, obj: dict[str, Any]) -> dict[str, Any]:
        raise NotImplementedError

    async def execute(self, call: ProviderCall) -> ProviderResult:
        timeout = make_timeout(call.timeouts.connect, call.timeouts.read)
        headers = {"Content-Type": "application/json", **_auth_headers(call)}
        if call.stream:
            headers["Accept"] = "text/event-stream"
        start = time.perf_counter()
        try:
            async with asyncio.timeout(call.timeouts.total):
                async with make_async_client(timeout=timeout, transport=self._transport) as client:
                    request = client.build_request(
                        "POST", call.url, params=call.query or None, headers=headers, content=json.dumps(call.body)
                    )
                    response = await client.send(request, stream=True)
                    try:
                        return await self._handle(call, response, start)
                    finally:
                        await response.aclose()
        except TimeoutError:
            return _error_result(call, start, error_type="timeout", message="Request exceeded the total timeout.")
        except httpx2.TimeoutException as exc:
            return _error_result(
                call, start, error_type="timeout", message=f"Request timed out ({type(exc).__name__})."
            )
        except httpx2.HTTPError as exc:
            return _error_result(call, start, error_type="network", message=f"Network error: {exc}")

    async def _handle(self, call: ProviderCall, response: httpx2.Response, start: float) -> ProviderResult:
        request_id = _request_id(response.headers)
        if response.status_code >= 400:
            raw = await response.aread()
            try:
                payload: Any = json.loads(raw)
            except ValueError:
                payload = raw.decode("utf-8", "replace")[:2000]
            error_type, finish, message = classify_error(response.status_code, payload)
            return _error_result(
                call,
                start,
                error_type=error_type,
                message=f"HTTP {response.status_code}: {message}",
                status=response.status_code,
                finish=finish,
                request_id=request_id,
                retry_after=response.headers.get("retry-after"),
                response_json=payload if isinstance(payload, dict) else None,
            )
        if call.stream and "text/event-stream" in response.headers.get("content-type", ""):
            return await self._handle_stream(call, response, start, request_id)
        raw = await response.aread()
        try:
            obj = json.loads(raw)
        except ValueError:
            return _error_result(
                call, start, error_type="bad_response", message="Provider returned invalid JSON.", status=200
            )
        return self._success(call, obj, start, request_id, response.status_code, ttft_ms=None, streamed=False)

    def _success(
        self,
        call: ProviderCall,
        obj: dict[str, Any],
        start: float,
        request_id: str | None,
        status: int,
        *,
        ttft_ms: int | None,
        streamed: bool,
    ) -> ProviderResult:
        parsed = self._parse(obj)
        failed = parsed["finish"] is FinishReason.ERROR
        return ProviderResult(
            ok=not failed,
            request_body=call.body,
            streamed=streamed,
            latency_ms=_ms(start),
            ttft_ms=ttft_ms,
            response_json=obj,
            raw_usage=parsed["usage"],
            output_text=parsed["text"],
            reasoning_summary=parsed["summary"],
            finish_reason=parsed["finish"],
            raw_finish_reason=None if parsed["raw_finish"] is None else str(parsed["raw_finish"]),
            response_model=parsed["model"],
            service_tier=parsed["service_tier"],
            request_id=request_id,
            http_status=status,
            error_type="provider_failed" if failed else None,
            error_message=sanitize_error_text(_failure_message(obj)) if failed else None,
        )

    async def _handle_stream(
        self, call: ProviderCall, response: httpx2.Response, start: float, request_id: str | None
    ) -> ProviderResult:
        raise NotImplementedError


def _failure_message(obj: dict[str, Any]) -> str:
    err = obj.get("error")
    if isinstance(err, dict):
        return str(err.get("message") or err)
    return "The provider reported a failed response."


class ResponsesAdapter(_OpenAICompatibleAdapter):
    api_style = ApiStyle.OPENAI_RESPONSES

    def _parse(self, obj: dict[str, Any]) -> dict[str, Any]:
        return parse_responses_object(obj)

    async def _handle_stream(
        self, call: ProviderCall, response: httpx2.Response, start: float, request_id: str | None
    ) -> ProviderResult:
        ttft: int | None = None
        deltas: list[str] = []
        final: dict[str, Any] | None = None
        error_payload: dict[str, Any] | None = None
        async for event in iter_sse(response):
            if event.data == "[DONE]":
                break
            try:
                payload = json.loads(event.data)
            except ValueError:
                continue
            kind = payload.get("type") or event.event
            if kind == "response.output_text.delta":
                if ttft is None:
                    ttft = _ms(start)
                deltas.append(str(payload.get("delta") or ""))
            elif kind in ("response.completed", "response.incomplete", "response.failed"):
                final = payload.get("response") if isinstance(payload.get("response"), dict) else None
            elif kind == "error":
                error_payload = payload
        if final is not None:
            result = self._success(call, final, start, request_id, response.status_code, ttft_ms=ttft, streamed=True)
            if not result.output_text and deltas:
                result.output_text = "".join(deltas)
            return result
        if error_payload is not None:
            error_type, finish, message = classify_error(None, error_payload)
            return _error_result(
                call,
                start,
                error_type=error_type,
                message=message,
                status=response.status_code,
                finish=finish,
                request_id=request_id,
                ttft_ms=ttft,
            )
        return _error_result(
            call,
            start,
            error_type="stream_incomplete",
            message="The stream ended before the final response (usage not reported).",
            status=response.status_code,
            request_id=request_id,
            ttft_ms=ttft,
        )


class ChatCompletionsAdapter(_OpenAICompatibleAdapter):
    api_style = ApiStyle.OPENAI_CHAT

    def _parse(self, obj: dict[str, Any]) -> dict[str, Any]:
        return parse_chat_object(obj)

    async def _handle_stream(
        self, call: ProviderCall, response: httpx2.Response, start: float, request_id: str | None
    ) -> ProviderResult:
        ttft: int | None = None
        content: list[str] = []
        refusal: list[str] = []
        finish: str | None = None
        usage: dict[str, Any] | None = None
        meta: dict[str, Any] = {}
        saw_done = False
        async for event in iter_sse(response):
            if event.data == "[DONE]":
                saw_done = True
                break
            try:
                chunk = json.loads(event.data)
            except ValueError:
                continue
            if isinstance(chunk.get("error"), dict):
                error_type, fin, error_text = classify_error(None, chunk)
                return _error_result(
                    call,
                    start,
                    error_type=error_type,
                    message=error_text,
                    status=response.status_code,
                    finish=fin,
                    request_id=request_id,
                    ttft_ms=ttft,
                )
            for key in ("id", "model", "created", "service_tier", "system_fingerprint"):
                if chunk.get(key) is not None:
                    meta[key] = chunk[key]
            if isinstance(chunk.get("usage"), dict):
                usage = chunk["usage"]
            for choice in chunk.get("choices") or []:
                delta = choice.get("delta") or {}
                piece = delta.get("content")
                if piece:
                    if ttft is None:
                        ttft = _ms(start)
                    content.append(str(piece))
                if delta.get("refusal"):
                    refusal.append(str(delta["refusal"]))
                if choice.get("finish_reason"):
                    finish = choice["finish_reason"]
        if finish is None and not saw_done:
            return _error_result(
                call,
                start,
                error_type="stream_incomplete",
                message="The stream ended before completion (usage not reported).",
                status=response.status_code,
                request_id=request_id,
                ttft_ms=ttft,
            )
        message: dict[str, Any] = {"role": "assistant", "content": "".join(content)}
        if refusal:
            message["refusal"] = "".join(refusal)
        obj: dict[str, Any] = {
            **meta,
            "object": "chat.completion",
            "choices": [{"index": 0, "message": message, "finish_reason": finish}],
            "usage": usage,
            "reconstructed_from_stream": True,
        }
        return self._success(call, obj, start, request_id, response.status_code, ttft_ms=ttft, streamed=True)
