"""Test helpers: build profiles/endpoints in memory and provider calls against the mock app."""

from __future__ import annotations

from typing import Any

from foundry_eval.domain.generation import GenerationSettings
from foundry_eval.domain.presets import preset_capabilities
from foundry_eval.domain.profiles import Endpoint, ModelProfile
from foundry_eval.providers.base import Prompt, ProviderCall
from foundry_eval.providers.request_builder import build_body, endpoint_url, profile_timeouts
from foundry_eval.types import ApiStyle

MOCK_BASE = "http://127.0.0.1:8766"


def endpoint(base_url: str = MOCK_BASE, auth_style: str = "api-key") -> Endpoint:
    return Endpoint(
        id="ep",
        name="mock",
        base_url=base_url,
        region="eastus2",
        auth_style=auth_style,  # type: ignore[arg-type]
        credential_ref="ref",
        key_set_at=None,
        created_at="t",
        updated_at="t",
    )


def profile(
    preset: str,
    api_style: ApiStyle,
    *,
    deployment: str = "gpt-5-mini",
    route: str | None = None,
    streaming: bool = True,
    include_usage: bool = True,
    **extra: Any,
) -> ModelProfile:
    route = route or ("anthropic" if api_style is ApiStyle.ANTHROPIC_MESSAGES else "openai_v1")
    return ModelProfile(
        id=f"p-{deployment}",
        endpoint_id="ep",
        name=deployment,
        api_style=api_style,
        route=route,  # type: ignore[arg-type]
        deployment=deployment,
        preset=preset,
        capabilities=preset_capabilities(preset, api_style),
        streaming=streaming,
        include_usage=include_usage,
        created_at="t",
        updated_at="t",
        **extra,
    )


def call_for(
    prof: ModelProfile,
    settings: GenerationSettings,
    prompt: Prompt,
    *,
    ep: Endpoint | None = None,
    key: str = "test-key-123456",
    json_schema: dict[str, Any] | None = None,
) -> ProviderCall:
    ep = ep or endpoint()
    url, query = endpoint_url(ep, prof)
    return ProviderCall(
        api_style=prof.api_style,
        url=url,
        query=query,
        auth_style=ep.auth_style,
        api_key=key,
        body=build_body(prof, settings, prompt, json_schema=json_schema),
        timeouts=profile_timeouts(prof),
    )
