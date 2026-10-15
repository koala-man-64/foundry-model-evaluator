from __future__ import annotations

import httpx2
import pytest

from foundry_eval.devtools.mock_foundry import create_mock_app
from foundry_eval.domain.generation import GenerationSettings
from foundry_eval.providers.anthropic_messages import AnthropicMessagesAdapter
from foundry_eval.providers.base import Prompt
from foundry_eval.providers.openai_compat import ChatCompletionsAdapter, ResponsesAdapter
from foundry_eval.providers.request_builder import SettingsValidationError, build_body
from foundry_eval.types import ApiStyle, FinishReason
from foundry_eval.usage.normalizers import normalize_usage

from ..support import call_for, endpoint, profile

R, C, A = ApiStyle.OPENAI_RESPONSES, ApiStyle.OPENAI_CHAT, ApiStyle.ANTHROPIC_MESSAGES
PROMPT = Prompt(system="You are concise.", user="Explain prompt caching in one paragraph.")


@pytest.fixture
def mock():
    app = create_mock_app()
    return app, httpx2.ASGITransport(app=app)


def stats(app) -> dict:
    state = app.state.mock
    return {"requests": dict(state.requests), "auth": state.auth_kinds}


@pytest.mark.parametrize("streaming", [True, False])
async def test_responses_adapter(mock, streaming):
    app, transport = mock
    prof = profile("openai-reasoning", R, deployment="gpt-5-mini", streaming=streaming)
    settings = GenerationSettings(max_output_tokens=2000, effort="high", reasoning_summary="auto", verbosity="low")
    result = await ResponsesAdapter(transport).execute(call_for(prof, settings, PROMPT))
    assert result.ok and result.finish_reason is FinishReason.COMPLETED
    assert result.streamed is streaming
    assert "answer (effort: high)" in result.output_text
    assert result.reasoning_summary == "Mock reasoning summary."
    assert result.request_id and result.request_id.startswith("mock-")
    assert (result.ttft_ms is not None) is streaming
    facts = normalize_usage(R, result.raw_usage)
    assert facts is not None and facts.reasoning == 1024
    assert result.request_body["store"] is False and result.request_body["truncation"] == "disabled"
    assert result.request_body["reasoning"] == {"effort": "high", "summary": "auto"}
    assert stats(app) == {"requests": {"/openai/v1/responses": 1}, "auth": {"/openai/v1/responses": "api-key"}}


async def test_responses_max_tokens_is_incomplete(mock):
    _, transport = mock
    prof = profile("openai-reasoning", R, streaming=False)
    result = await ResponsesAdapter(transport).execute(
        call_for(prof, GenerationSettings(max_output_tokens=5, effort="high"), PROMPT)
    )
    assert result.ok and result.finish_reason is FinishReason.MAX_TOKENS
    assert result.raw_finish_reason == "incomplete:max_output_tokens"


@pytest.mark.parametrize(("streaming", "include_usage"), [(True, True), (False, True), (True, False)])
async def test_chat_adapter_streaming_and_usage(mock, streaming, include_usage):
    _, transport = mock
    prof = profile("generic-chat", C, deployment="mistral-medium", streaming=streaming, include_usage=include_usage)
    result = await ChatCompletionsAdapter(transport).execute(
        call_for(prof, GenerationSettings(max_output_tokens=200, temperature=0.2), PROMPT)
    )
    assert result.ok and result.finish_reason is FinishReason.COMPLETED
    # Chat streams only when include_usage is on, so usage is always reported.
    assert result.streamed is (streaming and include_usage)
    assert result.raw_usage is not None and result.raw_usage["prompt_tokens"] > 0
    assert result.request_body.get("max_tokens") == 200
    assert result.request_body["messages"][0]["role"] == "system"


async def test_chat_reasoning_profile_uses_developer_role_and_effort(mock):
    _, transport = mock
    prof = profile("openai-reasoning", C, deployment="o4-mini", streaming=False)
    call = call_for(prof, GenerationSettings(max_output_tokens=500, effort="low"), PROMPT)
    assert call.body["messages"][0]["role"] == "developer"
    assert call.body["max_completion_tokens"] == 500 and call.body["reasoning_effort"] == "low"
    result = await ChatCompletionsAdapter(transport).execute(call)
    assert result.ok and normalize_usage(C, result.raw_usage).reasoning == 64


async def test_foundry_models_route_sends_api_version(mock):
    app, transport = mock
    prof = profile("generic-chat", C, deployment="Llama-4", route="foundry_models", streaming=False)
    call = call_for(prof, GenerationSettings(max_output_tokens=50), PROMPT)
    assert call.url.endswith("/models/chat/completions") and call.query == {"api-version": "2024-05-01-preview"}
    assert (await ChatCompletionsAdapter(transport).execute(call)).ok
    assert stats(app)["requests"] == {"/models/chat/completions": 1}


@pytest.mark.parametrize("streaming", [True, False])
async def test_claude_adapter_with_thinking_and_cache(mock, streaming):
    app, transport = mock
    prof = profile("claude-adaptive", A, deployment="claude-opus-test", streaming=streaming)
    settings = GenerationSettings(
        max_output_tokens=4000,
        thinking_mode="adaptive",
        thinking_display="summarized",
        effort="medium",
        prompt_cache_ttl="1h",
    )
    adapter = AnthropicMessagesAdapter(transport)
    first = await adapter.execute(call_for(prof, settings, PROMPT))
    assert first.ok, first.error_message
    assert first.finish_reason is FinishReason.COMPLETED
    assert first.reasoning_summary == "Mock thinking summary."
    assert "effort: medium" in first.output_text
    assert first.request_id and first.request_id.startswith("req_mock_")
    facts1 = normalize_usage(A, first.raw_usage)
    assert facts1.cache_write_1h and facts1.cache_write_5m == 0 and facts1.cache_read == 0
    assert facts1.reasoning == 256
    second = await adapter.execute(call_for(prof, settings, PROMPT))
    facts2 = normalize_usage(A, second.raw_usage)
    assert facts2.cache_read == facts1.cache_write_1h and facts2.cache_write == 0
    body = first.request_body
    assert body["thinking"] == {"type": "adaptive", "display": "summarized"}
    assert body["output_config"] == {"effort": "medium"}
    assert body["messages"][0]["content"][0]["cache_control"] == {"type": "ephemeral", "ttl": "1h"}
    assert stats(app)["auth"] == {"/anthropic/v1/messages": "x-api-key+api-key"}


async def test_claude_legacy_sampling_goes_through_extra_body(mock):
    _, transport = mock
    prof = profile("claude-legacy", A, deployment="claude-haiku-4-5", streaming=False)
    settings = GenerationSettings(max_output_tokens=3000, thinking_mode="enabled", thinking_budget_tokens=2048)
    result = await AnthropicMessagesAdapter(transport).execute(call_for(prof, settings, PROMPT))
    assert result.ok and result.request_body["thinking"] == {"type": "enabled", "budget_tokens": 2048}
    sampled = await AnthropicMessagesAdapter(transport).execute(
        call_for(prof, GenerationSettings(max_output_tokens=300, temperature=0.3, top_k=20), PROMPT)
    )
    assert sampled.ok and sampled.request_body["temperature"] == 0.3


@pytest.mark.parametrize(
    ("adapter_cls", "style", "preset", "path"),
    [
        (ResponsesAdapter, R, "openai-reasoning", "/openai/v1/responses"),
        (ChatCompletionsAdapter, C, "generic-chat", "/openai/v1/chat/completions"),
        (AnthropicMessagesAdapter, A, "claude-adaptive", "/anthropic/v1/messages"),
    ],
)
@pytest.mark.parametrize("failure", ["fail-500", "rate-limited"])
async def test_failures_are_single_requests_without_retry(mock, adapter_cls, style, preset, path, failure):
    app, transport = mock
    prof = profile(preset, style, deployment=failure, streaming=False)
    result = await adapter_cls(transport).execute(call_for(prof, GenerationSettings(max_output_tokens=100), PROMPT))
    assert not result.ok
    assert result.finish_reason is FinishReason.ERROR
    assert stats(app)["requests"] == {path: 1}
    if failure == "rate-limited":
        assert result.http_status == 429 and result.error_type == "rate_limited" and result.retry_after == "7"
    else:
        assert result.http_status == 500 and result.error_type == "provider_error"


async def test_stream_without_final_event_is_reported(mock):
    _, transport = mock
    prof = profile("openai-reasoning", R, deployment="no-usage", streaming=True)
    result = await ResponsesAdapter(transport).execute(
        call_for(prof, GenerationSettings(max_output_tokens=100), PROMPT)
    )
    assert not result.ok and result.error_type == "stream_incomplete"
    assert result.raw_usage is None


async def test_bearer_auth_style(mock):
    app, transport = mock
    prof = profile("generic-chat", C, deployment="x", streaming=False)
    call = call_for(prof, GenerationSettings(max_output_tokens=10), PROMPT, ep=endpoint(auth_style="bearer"))
    assert (await ChatCompletionsAdapter(transport).execute(call)).ok
    assert stats(app)["auth"] == {"/openai/v1/chat/completions": "bearer"}


async def test_missing_key_is_auth_error():
    app = create_mock_app(expected_key="right-key-123")
    transport = httpx2.ASGITransport(app=app)
    prof = profile("generic-chat", C, deployment="x", streaming=False)
    result = await ChatCompletionsAdapter(transport).execute(
        call_for(prof, GenerationSettings(max_output_tokens=10), PROMPT, key="wrong-key-456")
    )
    assert result.http_status == 401 and result.error_type == "auth"
    assert "wrong-key-456" not in (result.error_message or "")


async def test_network_error_is_captured():
    def boom(request: httpx2.Request) -> httpx2.Response:
        raise httpx2.ConnectError("connection refused")

    prof = profile("generic-chat", C, deployment="x", streaming=False)
    result = await ChatCompletionsAdapter(httpx2.MockTransport(boom)).execute(
        call_for(prof, GenerationSettings(max_output_tokens=10), PROMPT)
    )
    assert not result.ok and result.error_type == "network"


# ---------------------------------------------------------------------------------------------
def test_unsupported_settings_are_rejected_not_dropped():
    prof = profile("openai-reasoning", R)
    with pytest.raises(SettingsValidationError) as excinfo:
        build_body(prof, GenerationSettings(max_output_tokens=10, temperature=0.5), PROMPT)
    assert excinfo.value.issues[0].setting == "temperature"
    with pytest.raises(SettingsValidationError):
        build_body(prof, GenerationSettings(effort="ultra"), PROMPT)


def test_setting_without_api_mapping_is_rejected():
    from foundry_eval.domain.capabilities import SettingSpec

    prof = profile("openai-reasoning", R)
    caps = prof.capabilities.model_copy(
        update={"settings": {**prof.capabilities.settings, "stop": SettingSpec(supported=True)}}
    )
    forced = prof.model_copy(update={"capabilities": caps})
    with pytest.raises(SettingsValidationError, match="cannot be sent"):
        build_body(forced, GenerationSettings(stop=["END"]), PROMPT)


def test_claude_requires_max_tokens_and_cross_field_rules():
    prof = profile("claude-legacy", A)
    with pytest.raises(SettingsValidationError, match="required"):
        build_body(prof, GenerationSettings(), PROMPT)
    with pytest.raises(SettingsValidationError, match="lower than max output"):
        build_body(
            prof,
            GenerationSettings(max_output_tokens=2000, thinking_mode="enabled", thinking_budget_tokens=4000),
            PROMPT,
        )
    with pytest.raises(SettingsValidationError, match="cannot be combined"):
        build_body(
            prof,
            GenerationSettings(
                max_output_tokens=4000, thinking_mode="enabled", thinking_budget_tokens=2000, temperature=0.5
            ),
            PROMPT,
        )


def test_structured_output_parameters_per_style():
    schema = {"type": "object", "properties": {}, "required": [], "additionalProperties": False}
    r = build_body(profile("openai-reasoning", R), GenerationSettings(), PROMPT, json_schema=schema)
    assert r["text"]["format"]["type"] == "json_schema" and r["text"]["format"]["strict"] is True
    c = build_body(profile("generic-chat", C), GenerationSettings(), PROMPT, json_schema=schema)
    assert c["response_format"]["json_schema"]["schema"] == schema
    a = build_body(profile("claude-adaptive", A), GenerationSettings(max_output_tokens=100), PROMPT, json_schema=schema)
    assert a["output_config"]["format"] == {"type": "json_schema", "schema": schema}
