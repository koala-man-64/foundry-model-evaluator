"""Build exact provider request bodies from provider-neutral settings.

Every provided setting must map to a provider parameter; a setting that cannot be expressed for
the selected API raises an error instead of being dropped.
"""

from __future__ import annotations

from typing import Any

from ..domain.generation import GenerationSettings, SettingIssue, validate_settings
from ..domain.profiles import DEFAULT_FOUNDRY_MODELS_API_VERSION, Endpoint, ModelProfile
from ..types import ApiStyle
from .base import Prompt, Timeouts

_ANTHROPIC_TTL = {"5m": {"type": "ephemeral"}, "1h": {"type": "ephemeral", "ttl": "1h"}}


class SettingsValidationError(ValueError):
    def __init__(self, issues: list[SettingIssue]) -> None:
        super().__init__("; ".join(i.message for i in issues))
        self.issues = issues


def endpoint_url(endpoint: Endpoint, profile: ModelProfile) -> tuple[str, dict[str, str]]:
    base = endpoint.base_url.rstrip("/")
    query: dict[str, str] = {}
    if profile.route == "cli":
        return base, query
    if profile.route == "custom":
        path = profile.custom_path or ""
        if profile.api_version:
            query["api-version"] = profile.api_version
        return base + path, query
    if profile.route == "anthropic":
        return base + "/anthropic", query
    if profile.route == "foundry_models":
        query["api-version"] = profile.api_version or DEFAULT_FOUNDRY_MODELS_API_VERSION
        return base + "/models/chat/completions", query
    suffix = "/responses" if profile.api_style is ApiStyle.OPENAI_RESPONSES else "/chat/completions"
    return base + "/openai/v1" + suffix, query


def profile_timeouts(profile: ModelProfile) -> Timeouts:
    return Timeouts(connect=profile.timeout_connect_s, read=profile.timeout_read_s, total=profile.timeout_total_s)


def _unsupported(api_style: ApiStyle, keys: list[str]) -> None:
    if keys:
        issues = [
            SettingIssue(setting=k, message=f"'{k}' cannot be sent with the {api_style.value} API.") for k in keys
        ]
        raise SettingsValidationError(issues)


def build_body(
    profile: ModelProfile,
    settings: GenerationSettings,
    prompt: Prompt,
    *,
    json_schema: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Validate settings against the profile and return the exact request body to send."""
    issues = validate_settings(settings, profile.capabilities)
    if issues:
        raise SettingsValidationError(issues)
    values = settings.provided()
    caps = profile.capabilities
    style = profile.api_style

    if style is ApiStyle.OPENAI_RESPONSES:
        _unsupported(
            style,
            [
                k
                for k in values
                if k
                in (
                    "stop",
                    "seed",
                    "top_k",
                    "thinking_mode",
                    "thinking_budget_tokens",
                    "thinking_display",
                    "prompt_cache_ttl",
                )
            ],
        )
        body: dict[str, Any] = {
            "model": profile.request_model,
            "input": [{"role": "user", "content": [{"type": "input_text", "text": prompt.user}]}],
            "store": False,
            "truncation": "disabled",
        }
        if prompt.system:
            body["instructions"] = prompt.system
        if "max_output_tokens" in values:
            body["max_output_tokens"] = values["max_output_tokens"]
        for key in ("temperature", "top_p"):
            if key in values:
                body[key] = values[key]
        reasoning: dict[str, Any] = {}
        if "effort" in values:
            reasoning["effort"] = values["effort"]
        if "reasoning_summary" in values:
            reasoning["summary"] = values["reasoning_summary"]
        if reasoning:
            body["reasoning"] = reasoning
        text: dict[str, Any] = {}
        if "verbosity" in values:
            text["verbosity"] = values["verbosity"]
        if json_schema is not None:
            text["format"] = {"type": "json_schema", "name": "judge_result", "schema": json_schema, "strict": True}
        if text:
            body["text"] = text
        if profile.streaming:
            body["stream"] = True
        return body

    if style is ApiStyle.OPENAI_CHAT:
        _unsupported(
            style,
            [
                k
                for k in values
                if k
                in (
                    "top_k",
                    "thinking_mode",
                    "thinking_budget_tokens",
                    "thinking_display",
                    "reasoning_summary",
                    "prompt_cache_ttl",
                )
            ],
        )
        messages: list[dict[str, Any]] = []
        if prompt.system:
            messages.append({"role": caps.system_role, "content": prompt.system})
        messages.append({"role": "user", "content": prompt.user})
        body = {"model": profile.request_model, "messages": messages}
        if "max_output_tokens" in values:
            param = caps.max_output_param if caps.max_output_param != "max_output_tokens" else "max_tokens"
            body[param] = values["max_output_tokens"]
        for key in ("temperature", "top_p", "stop", "seed"):
            if key in values:
                body[key] = values[key]
        if "effort" in values:
            body["reasoning_effort"] = values["effort"]
        if "verbosity" in values:
            body["verbosity"] = values["verbosity"]
        if json_schema is not None:
            body["response_format"] = {
                "type": "json_schema",
                "json_schema": {"name": "judge_result", "schema": json_schema, "strict": True},
            }
        # Streaming Chat Completions report usage only with include_usage; otherwise do not stream.
        if profile.streaming and profile.include_usage:
            body["stream"] = True
            body["stream_options"] = {"include_usage": True}
        return body

    if style is ApiStyle.CLAUDE_CLI:
        _unsupported(
            style,
            [
                k
                for k in values
                if k
                in (
                    "max_output_tokens",
                    "temperature",
                    "top_p",
                    "top_k",
                    "stop",
                    "seed",
                    "thinking_mode",
                    "thinking_budget_tokens",
                    "thinking_display",
                    "reasoning_summary",
                    "verbosity",
                    "prompt_cache_ttl",
                )
            ],
        )
        body = {
            "cli": "claude",
            "model": profile.request_model,
            "prompt": prompt.user,
            "stream": False,
        }
        if prompt.system:
            body["system_prompt"] = prompt.system
        if "effort" in values:
            body["effort"] = values["effort"]
        if json_schema is not None:
            body["json_schema"] = json_schema
        return body

    if style is ApiStyle.CODEX_CLI:
        _unsupported(
            style,
            [
                k
                for k in values
                if k
                in (
                    "max_output_tokens",
                    "temperature",
                    "top_p",
                    "top_k",
                    "stop",
                    "seed",
                    "thinking_mode",
                    "thinking_budget_tokens",
                    "thinking_display",
                    "verbosity",
                    "prompt_cache_ttl",
                )
            ],
        )
        body = {
            "cli": "codex",
            "model": profile.request_model,
            "prompt": prompt.user,
            "stream": True,
        }
        if prompt.system:
            body["developer_instructions"] = prompt.system
        if "effort" in values:
            body["model_reasoning_effort"] = values["effort"]
        if "reasoning_summary" in values:
            body["model_reasoning_summary"] = values["reasoning_summary"]
        if json_schema is not None:
            body["output_schema"] = json_schema
        return body

    # Claude Messages API
    _unsupported(style, [k for k in values if k in ("seed", "reasoning_summary", "verbosity")])
    block: dict[str, Any] = {"type": "text", "text": prompt.user}
    ttl = values.get("prompt_cache_ttl")
    if ttl:
        block["cache_control"] = dict(_ANTHROPIC_TTL[ttl])
    body = {
        "model": profile.request_model,
        "max_tokens": values["max_output_tokens"],
        "messages": [{"role": "user", "content": [block]}],
    }
    if prompt.system:
        body["system"] = prompt.system
    mode = values.get("thinking_mode")
    if mode:
        thinking: dict[str, Any] = {"type": mode}
        if mode == "enabled":
            thinking["budget_tokens"] = values["thinking_budget_tokens"]
        if "thinking_display" in values and mode in ("adaptive", "enabled"):
            thinking["display"] = values["thinking_display"]
        body["thinking"] = thinking
    elif "thinking_display" in values:
        body["thinking"] = {"type": "adaptive", "display": values["thinking_display"]}
    output_config: dict[str, Any] = {}
    if "effort" in values:
        output_config["effort"] = values["effort"]
    if json_schema is not None:
        output_config["format"] = {"type": "json_schema", "schema": json_schema}
    if output_config:
        body["output_config"] = output_config
    for key in ("temperature", "top_p", "top_k"):
        if key in values:
            body[key] = values[key]
    if "stop" in values:
        body["stop_sequences"] = values["stop"]
    if profile.streaming:
        body["stream"] = True
    return body


def requested_cache_ttl(settings: GenerationSettings) -> str | None:
    return settings.prompt_cache_ttl
