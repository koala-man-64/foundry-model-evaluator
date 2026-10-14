"""Mock Microsoft Foundry endpoints for tests and no-cost demos.

Implements just enough of:

* the free model list ``GET /openai/v1/models`` (used by the endpoint "verify" action);
* OpenAI-compatible ``POST /openai/v1/responses`` and ``/openai/v1/chat/completions`` (and the
  Foundry Models route ``/models/chat/completions``), JSON or SSE streaming;
* Claude ``POST /anthropic/v1/messages`` (JSON or SSE), including prompt-cache simulation;
* Azure Retail Prices ``GET /api/retail/prices`` (paged) from a bundled demo catalog.

Deployment names trigger special behaviour: ``fail-500``, ``rate-limited``, ``slow`` (or
``slow-<seconds>``), ``no-usage``, ``judge-malformed``. ``GET /__mock/stats`` reports request counts
and which auth header was used (keys are never echoed, only their SHA-256 prefixes).

Run: ``python -m foundry_eval.devtools.mock_foundry --port 8766``
"""

from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import math
import re
from collections import Counter
from collections.abc import AsyncIterator
from importlib import resources
from typing import Any

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse, Response, StreamingResponse

EFFORT_REASONING = {"none": 0, "minimal": 16, "low": 64, "medium": 256, "high": 1024, "xhigh": 2048, "max": 4096}
_LABEL_RE = re.compile(r"=== Response ([A-Z]{1,2}) \(begin")
_CRITERION_RE = re.compile(r"^- key: ([a-z0-9_]+)", re.MULTILINE)


def _sse(event: str | None, data: Any) -> str:
    payload = data if isinstance(data, str) else json.dumps(data)
    return (f"event: {event}\n" if event else "") + f"data: {payload}\n\n"


def _tokens(text: str) -> int:
    return max(1, math.ceil(len(text) / 4))


def _hash(value: str) -> str:
    return hashlib.sha256(value.encode()).hexdigest()


class MockState:
    def __init__(self, expected_key: str | None = None) -> None:
        self.expected_key = expected_key
        self.requests: Counter[str] = Counter()
        self.auth_kinds: dict[str, str] = {}
        self.key_hashes: set[str] = set()
        self.seen_prompts: set[str] = set()
        self.cache_prefixes: set[str] = set()
        self.cancelled = 0

    def reset(self) -> None:
        self.requests.clear()
        self.auth_kinds.clear()
        self.key_hashes.clear()
        self.seen_prompts.clear()
        self.cache_prefixes.clear()
        self.cancelled = 0


def _auth(state: MockState, request: Request, path: str) -> Response | None:
    key = request.headers.get("api-key") or request.headers.get("x-api-key")
    kind = "+".join(h for h in ("x-api-key", "api-key") if request.headers.get(h))
    auth = request.headers.get("authorization", "")
    if not key and auth.lower().startswith("bearer "):
        key, kind = auth[7:], "bearer"
    if not key:
        return JSONResponse({"error": {"code": "401", "message": "Missing API key."}}, status_code=401)
    state.auth_kinds[path] = kind
    state.key_hashes.add(_hash(key)[:16])
    if state.expected_key is not None and key != state.expected_key:
        return JSONResponse({"error": {"code": "401", "message": "Invalid API key."}}, status_code=401)
    return None


async def _special(deployment: str) -> Response | None:
    if "fail-500" in deployment:
        return JSONResponse({"error": {"code": "InternalServerError", "message": "Mock failure."}}, status_code=500)
    if "rate-limited" in deployment:
        return JSONResponse(
            {"error": {"code": "429", "message": "Rate limit reached."}}, status_code=429, headers={"retry-after": "7"}
        )
    if "slow" in deployment:
        match = re.search(r"slow-(\d+)", deployment)
        await asyncio.sleep(float(match.group(1)) if match else 3.0)
    return None


def _judge_output(schema: dict[str, Any] | None, prompt_text: str, deployment: str) -> str | None:
    if "judge-malformed" in deployment:
        return "I think response A is better, but I cannot provide JSON."
    labels: list[str] = []
    criteria: list[str] = []
    if schema:
        try:
            items = schema["properties"]["responses"]["items"]
            labels = list(items["properties"]["label"]["enum"])
            criteria = list(items["properties"]["criteria"]["items"]["properties"]["key"]["enum"])
        except (KeyError, TypeError):
            labels, criteria = [], []
    if not labels and "=== Response " in prompt_text:
        labels = _LABEL_RE.findall(prompt_text)
        criteria = _CRITERION_RE.findall(prompt_text)
    if not labels:
        return None
    responses: list[dict[str, Any]] = []
    for index, label in enumerate(labels):
        values = [5 - ((index + offset) % 3) for offset in range(len(criteria))]
        scores = [
            {"key": key, "score": value, "explanation": f"{key} assessment for {label}."}
            for key, value in zip(criteria, values, strict=True)
        ]
        overall = round(sum(values) / max(1, len(values)), 2)
        responses.append(
            {"label": label, "criteria": scores, "overall": overall, "explanation": f"Summary for {label}."}
        )
    best = max(responses, key=lambda r: r["overall"])
    tied = [r for r in responses if r["overall"] == best["overall"]]
    return json.dumps(
        {
            "responses": responses,
            "preferred": "tie" if len(tied) > 1 else best["label"],
            "preference_explanation": "Mock judge preference.",
        }
    )


def _answer(deployment: str, effort: str | None, user_text: str) -> str:
    snippet = " ".join(user_text.split())[:80]
    return (
        f"Mock answer from {deployment} (effort: {effort or 'default'}). "
        f"You asked: “{snippet}”. Here is a concise, well-structured reply."
    )


def create_mock_app(expected_key: str | None = None) -> FastAPI:
    app = FastAPI(title="Mock Foundry")
    state = MockState(expected_key)
    app.state.mock = state

    @app.get("/__mock/stats")
    async def stats() -> dict[str, Any]:
        return {
            "requests": dict(state.requests),
            "auth_kinds": state.auth_kinds,
            "key_hashes": sorted(state.key_hashes),
            "cancelled": state.cancelled,
        }

    @app.post("/__mock/reset")
    async def reset() -> dict[str, str]:
        state.reset()
        return {"status": "ok"}

    @app.get("/openai/v1/models")
    async def models(request: Request) -> Response:
        path = "/openai/v1/models"
        state.requests[path] += 1
        if (denied := _auth(state, request, path)) is not None:
            return denied
        return JSONResponse({"object": "list", "data": [{"id": "mock-model", "object": "model"}]})

    # ------------------------------------------------------------------ OpenAI Responses
    @app.post("/openai/v1/responses")
    async def responses(request: Request) -> Response:
        path = "/openai/v1/responses"
        state.requests[path] += 1
        if (denied := _auth(state, request, path)) is not None:
            return denied
        body = await request.json()
        deployment = str(body.get("model", ""))
        if (special := await _special(deployment)) is not None:
            return special
        instructions = str(body.get("instructions") or "")
        user = "".join(
            part.get("text", "")
            for message in body.get("input") or []
            for part in (message.get("content") or [])
            if isinstance(part, dict)
        )
        effort = (body.get("reasoning") or {}).get("effort")
        schema = ((body.get("text") or {}).get("format") or {}).get("schema")
        text = _judge_output(schema, instructions + user, deployment) if schema or "judge" in deployment else None
        text = text if text is not None else _answer(deployment, effort, user)
        input_tokens = _tokens(instructions + user) + 8
        prompt_key = _hash(instructions + user + deployment)
        cached = (input_tokens // 2) if prompt_key in state.seen_prompts else 0
        cache_write = input_tokens - 8 if ("5.6" in deployment and prompt_key not in state.seen_prompts) else 0
        state.seen_prompts.add(prompt_key)
        reasoning = EFFORT_REASONING.get(str(effort), 0) if effort else 0
        output_tokens = _tokens(text) + reasoning
        max_out = body.get("max_output_tokens")
        status, incomplete = "completed", None
        if isinstance(max_out, int) and output_tokens > max_out:
            output_tokens, status, incomplete = max_out, "incomplete", {"reason": "max_output_tokens"}
        details_in: dict[str, int] = {"cached_tokens": cached}
        if "5.6" in deployment:
            details_in["cache_write_tokens"] = cache_write
        usage = (
            None
            if "no-usage" in deployment
            else {
                "input_tokens": input_tokens,
                "input_tokens_details": details_in,
                "output_tokens": output_tokens,
                "output_tokens_details": {"reasoning_tokens": min(reasoning, output_tokens)},
                "total_tokens": input_tokens + output_tokens,
            }
        )
        output: list[dict[str, Any]] = []
        if (body.get("reasoning") or {}).get("summary"):
            output.append(
                {
                    "type": "reasoning",
                    "id": "rs_1",
                    "summary": [{"type": "summary_text", "text": "Mock reasoning summary."}],
                }
            )
        output.append(
            {
                "type": "message",
                "id": "msg_1",
                "role": "assistant",
                "status": "completed",
                "content": [{"type": "output_text", "text": text, "annotations": []}],
            }
        )
        response_obj = {
            "id": "resp_mock",
            "object": "response",
            "status": status,
            "model": deployment,
            "output": output,
            "usage": usage,
            "incomplete_details": incomplete,
            "service_tier": "default",
        }
        headers = {"x-request-id": "mock-" + prompt_key[:12]}
        if not body.get("stream"):
            return JSONResponse(response_obj, headers=headers)

        async def events() -> AsyncIterator[str]:
            yield _sse(
                "response.created",
                {
                    "type": "response.created",
                    "response": {**response_obj, "status": "in_progress", "output": [], "usage": None},
                },
            )
            for chunk in re.findall(r".{1,24}", text, flags=re.DOTALL):
                yield _sse("response.output_text.delta", {"type": "response.output_text.delta", "delta": chunk})
                await asyncio.sleep(0)
            final_type = "response.completed" if status == "completed" else "response.incomplete"
            if "no-usage" in deployment:
                return
            yield _sse(final_type, {"type": final_type, "response": response_obj})

        return StreamingResponse(events(), media_type="text/event-stream", headers=headers)

    # ------------------------------------------------------------------ OpenAI Chat Completions
    async def chat(request: Request, path: str) -> Response:
        state.requests[path] += 1
        if (denied := _auth(state, request, path)) is not None:
            return denied
        body = await request.json()
        deployment = str(body.get("model", ""))
        if (special := await _special(deployment)) is not None:
            return special
        messages = body.get("messages") or []
        system = "".join(str(m.get("content", "")) for m in messages if m.get("role") in ("system", "developer"))
        user = "".join(str(m.get("content", "")) for m in messages if m.get("role") == "user")
        effort = body.get("reasoning_effort")
        schema = ((body.get("response_format") or {}).get("json_schema") or {}).get("schema")
        text = _judge_output(schema, system + user, deployment) if schema or "judge" in deployment else None
        text = text if text is not None else _answer(deployment, effort, user)
        prompt_tokens = _tokens(system + user) + 6
        prompt_key = _hash(system + user + deployment)
        cached = (prompt_tokens // 2) if prompt_key in state.seen_prompts else 0
        state.seen_prompts.add(prompt_key)
        reasoning = EFFORT_REASONING.get(str(effort), 0) if effort else 0
        completion_tokens = _tokens(text) + reasoning
        usage = {
            "prompt_tokens": prompt_tokens,
            "completion_tokens": completion_tokens,
            "total_tokens": prompt_tokens + completion_tokens,
            "prompt_tokens_details": {"cached_tokens": cached, "audio_tokens": 0},
            "completion_tokens_details": {"reasoning_tokens": reasoning, "audio_tokens": 0},
        }
        headers = {"apim-request-id": "mock-" + prompt_key[:12]}
        if not body.get("stream"):
            return JSONResponse(
                {
                    "id": "chatcmpl-mock",
                    "object": "chat.completion",
                    "model": deployment,
                    "choices": [
                        {"index": 0, "message": {"role": "assistant", "content": text}, "finish_reason": "stop"}
                    ],
                    "usage": usage,
                },
                headers=headers,
            )
        include_usage = bool((body.get("stream_options") or {}).get("include_usage"))

        async def events() -> AsyncIterator[str]:
            for chunk in re.findall(r".{1,24}", text, flags=re.DOTALL):
                data = {
                    "id": "chatcmpl-mock",
                    "object": "chat.completion.chunk",
                    "model": deployment,
                    "choices": [{"index": 0, "delta": {"content": chunk}, "finish_reason": None}],
                }
                yield _sse(None, data)
                await asyncio.sleep(0)
            done = {
                "id": "chatcmpl-mock",
                "object": "chat.completion.chunk",
                "model": deployment,
                "choices": [{"index": 0, "delta": {}, "finish_reason": "stop"}],
            }
            yield _sse(None, done)
            if include_usage and "no-usage" not in deployment:
                yield _sse(
                    None,
                    {
                        "id": "chatcmpl-mock",
                        "object": "chat.completion.chunk",
                        "model": deployment,
                        "choices": [],
                        "usage": usage,
                    },
                )
            yield _sse(None, "[DONE]")

        return StreamingResponse(events(), media_type="text/event-stream", headers=headers)

    @app.post("/openai/v1/chat/completions")
    async def chat_v1(request: Request) -> Response:
        return await chat(request, "/openai/v1/chat/completions")

    @app.post("/models/chat/completions")
    async def chat_models(request: Request) -> Response:
        if not request.query_params.get("api-version"):
            return JSONResponse(
                {"error": {"code": "MissingApiVersion", "message": "api-version required"}}, status_code=400
            )
        return await chat(request, "/models/chat/completions")

    # ------------------------------------------------------------------ Claude Messages
    @app.post("/anthropic/v1/messages")
    async def messages(request: Request) -> Response:
        path = "/anthropic/v1/messages"
        state.requests[path] += 1
        if (denied := _auth(state, request, path)) is not None:
            return denied
        body = await request.json()
        deployment = str(body.get("model", ""))
        if (special := await _special(deployment)) is not None:
            return special
        system = body.get("system") or ""
        if isinstance(system, list):
            system = "".join(b.get("text", "") for b in system if isinstance(b, dict))
        blocks = [b for m in body.get("messages") or [] for b in (m.get("content") or []) if isinstance(b, dict)]
        user = "".join(b.get("text", "") for b in blocks)
        ttl = next((b["cache_control"].get("ttl", "5m") for b in blocks if b.get("cache_control")), None)
        effort = (body.get("output_config") or {}).get("effort")
        schema = ((body.get("output_config") or {}).get("format") or {}).get("schema")
        text = _judge_output(schema, str(system) + user, deployment) if schema or "judge" in deployment else None
        text = text if text is not None else _answer(deployment, effort, user)
        total_in = _tokens(str(system) + user) + 5
        uncached, read, write5, write1 = total_in, 0, 0, 0
        if ttl:
            prefix = _hash(str(system) + user + deployment)
            cacheable = max(0, total_in - 3)
            uncached = total_in - cacheable
            if prefix in state.cache_prefixes:
                read = cacheable
            elif ttl == "1h":
                write1 = cacheable
            else:
                write5 = cacheable
            state.cache_prefixes.add(prefix)
        thinking = body.get("thinking") or {}
        thinking_tokens = 0
        if thinking.get("type") in ("adaptive", "enabled"):
            thinking_tokens = EFFORT_REASONING.get(str(effort), 128) if effort else 128
        output_tokens = _tokens(text) + thinking_tokens
        stop_reason = "end_turn"
        if output_tokens > int(body.get("max_tokens") or 10**9):
            output_tokens, stop_reason = int(body["max_tokens"]), "max_tokens"
        usage: dict[str, Any] = {
            "input_tokens": uncached,
            "cache_creation_input_tokens": write5 + write1,
            "cache_read_input_tokens": read,
            "cache_creation": {"ephemeral_5m_input_tokens": write5, "ephemeral_1h_input_tokens": write1},
            "output_tokens": output_tokens,
            "service_tier": "standard",
        }
        if thinking_tokens:
            usage["output_tokens_details"] = {"thinking_tokens": thinking_tokens}
        content: list[dict[str, Any]] = []
        show_thinking = thinking.get("display") == "summarized"
        if show_thinking:
            content.append({"type": "thinking", "thinking": "Mock thinking summary.", "signature": "sig"})
        content.append({"type": "text", "text": text})
        message = {
            "id": "msg_mock",
            "type": "message",
            "role": "assistant",
            "model": deployment,
            "content": content,
            "stop_reason": stop_reason,
            "stop_sequence": None,
            "usage": usage,
        }
        headers = {"request-id": "req_mock_" + _hash(user)[:10]}
        if not body.get("stream"):
            return JSONResponse(message, headers=headers)

        async def events() -> AsyncIterator[str]:
            start_usage = {**usage, "output_tokens": 1}
            start = {**message, "content": [], "stop_reason": None, "usage": start_usage}
            yield _sse("message_start", {"type": "message_start", "message": start})
            index = 0
            if show_thinking:
                yield _sse(
                    "content_block_start",
                    {
                        "type": "content_block_start",
                        "index": 0,
                        "content_block": {"type": "thinking", "thinking": "", "signature": ""},
                    },
                )
                yield _sse(
                    "content_block_delta",
                    {
                        "type": "content_block_delta",
                        "index": 0,
                        "delta": {"type": "thinking_delta", "thinking": "Mock thinking summary."},
                    },
                )
                yield _sse(
                    "content_block_delta",
                    {
                        "type": "content_block_delta",
                        "index": 0,
                        "delta": {"type": "signature_delta", "signature": "sig"},
                    },
                )
                yield _sse("content_block_stop", {"type": "content_block_stop", "index": 0})
                index = 1
            yield _sse(
                "content_block_start",
                {"type": "content_block_start", "index": index, "content_block": {"type": "text", "text": ""}},
            )
            for chunk in re.findall(r".{1,24}", text, flags=re.DOTALL):
                yield _sse(
                    "content_block_delta",
                    {"type": "content_block_delta", "index": index, "delta": {"type": "text_delta", "text": chunk}},
                )
                await asyncio.sleep(0)
            yield _sse("content_block_stop", {"type": "content_block_stop", "index": index})
            delta_usage: dict[str, Any] = {
                "output_tokens": output_tokens,
                "input_tokens": uncached,
                "cache_creation_input_tokens": write5 + write1,
                "cache_read_input_tokens": read,
            }
            if thinking_tokens:
                delta_usage["output_tokens_details"] = {"thinking_tokens": thinking_tokens}
            yield _sse(
                "message_delta",
                {
                    "type": "message_delta",
                    "delta": {"stop_reason": stop_reason, "stop_sequence": None},
                    "usage": delta_usage,
                },
            )
            yield _sse("message_stop", {"type": "message_stop"})

        return StreamingResponse(events(), media_type="text/event-stream", headers=headers)

    # ------------------------------------------------------------------ Azure Retail Prices
    catalog = json.loads((resources.files("foundry_eval.devtools") / "demo_catalog.json").read_text(encoding="utf-8"))

    @app.get("/api/retail/prices")
    async def retail(request: Request) -> Response:
        path = "/api/retail/prices"
        state.requests[path] += 1
        filter_text = request.query_params.get("$filter", "")
        region_match = re.search(r"armRegionName eq '([a-z0-9]+)'", filter_text)
        region = region_match.group(1) if region_match else ""
        if "Foundry Models" not in filter_text:
            return JSONResponse({"Items": [], "NextPageLink": None, "Count": 0})
        items = [dict(i, armRegionName=region) for i in catalog["Items"]] if region else []
        skip = int(request.query_params.get("$skip", "0"))
        page = items[skip : skip + 20]
        next_link = None
        if skip + 20 < len(items):
            next_link = str(request.url.include_query_params(**{"$skip": str(skip + 20)}))
        return JSONResponse({"BillingCurrency": "USD", "Items": page, "NextPageLink": next_link, "Count": len(page)})

    return app


def main() -> None:
    import uvicorn

    parser = argparse.ArgumentParser(description="Mock Foundry endpoints for tests and demos")
    parser.add_argument("--port", type=int, default=8766)
    parser.add_argument("--expected-key", default=None)
    args = parser.parse_args()
    uvicorn.run(create_mock_app(args.expected_key), host="127.0.0.1", port=args.port, log_level="warning")


if __name__ == "__main__":
    main()
