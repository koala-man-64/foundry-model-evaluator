"""Endpoint configuration and write-only API key management.

Keys are accepted only by ``PUT /api/endpoints/{id}/key`` and are stored in the OS credential
store. No response ever contains a key; responses only report whether one is configured.
"""

from __future__ import annotations

from typing import Literal

import httpx2
from fastapi import APIRouter, Response
from pydantic import BaseModel, ConfigDict, Field

from ...credentials.redaction import sanitize_error_text
from ...domain.presets import preset_capabilities
from ...domain.profiles import AuthStyle, Endpoint, ProfileFields
from ...providers.http import make_async_client, make_timeout, redirect_message
from ...providers.local_cli import verify_local_cli
from ...types import ApiStyle
from ..deps import ServicesDep

router = APIRouter(prefix="/api/endpoints", tags=["endpoints"])


class EndpointIn(BaseModel):
    model_config = ConfigDict(extra="forbid")

    name: str = Field(min_length=1, max_length=120)
    base_url: str = Field(min_length=8, max_length=500)
    region: str = Field(default="", max_length=60)
    auth_style: AuthStyle = "api-key"


class EndpointOut(BaseModel):
    id: str
    name: str
    base_url: str
    region: str
    auth_style: AuthStyle
    key_configured: bool
    key_set_at: str | None
    created_at: str
    updated_at: str


class KeyIn(BaseModel):
    model_config = ConfigDict(extra="forbid")

    api_key: str = Field(min_length=1, max_length=2048)


class KeyStatus(BaseModel):
    key_configured: bool


class VerifyResult(BaseModel):
    ok: bool
    status: int | None
    message: str


class SeedLocalCliOut(BaseModel):
    endpoints_created: int
    profiles_created: int
    endpoints: list[EndpointOut]


def _out(services: ServicesDep, endpoint: Endpoint) -> EndpointOut:
    return EndpointOut(
        id=endpoint.id,
        name=endpoint.name,
        base_url=endpoint.base_url,
        region=endpoint.region,
        auth_style=endpoint.auth_style,
        key_configured=endpoint.auth_style == "local_cli" or services.credentials.has(endpoint.credential_ref),
        key_set_at=endpoint.key_set_at,
        created_at=endpoint.created_at,
        updated_at=endpoint.updated_at,
    )


@router.get("", response_model=list[EndpointOut])
def list_endpoints(services: ServicesDep) -> list[EndpointOut]:
    return [_out(services, e) for e in services.endpoints.list()]


@router.post("", response_model=EndpointOut, status_code=201)
def create_endpoint(payload: EndpointIn, services: ServicesDep) -> EndpointOut:
    endpoint = services.endpoints.create(**payload.model_dump())
    return _out(services, endpoint)


@router.post("/seed-local-cli", response_model=SeedLocalCliOut)
def seed_local_cli_endpoints(services: ServicesDep) -> SeedLocalCliOut:
    """Idempotently seed Local Claude CLI and Local Codex CLI endpoints and starter model profiles."""
    existing_endpoints = {e.base_url.lower(): e for e in services.endpoints.list()}
    endpoints_created = 0
    profiles_created = 0

    cli_specs: list[tuple[str, str, list[tuple[str, ApiStyle, str, str, str, str, int, int]]]] = [
        (
            "Local Claude CLI",
            "local://claude",
            [
                (
                    "Claude Sonnet (CLI)",
                    ApiStyle.CLAUDE_CLI,
                    "claude-cli",
                    "sonnet",
                    "Anthropic",
                    "Claude Sonnet",
                    200000,
                    16384,
                ),
                (
                    "Claude Opus (CLI)",
                    ApiStyle.CLAUDE_CLI,
                    "claude-cli",
                    "opus",
                    "Anthropic",
                    "Claude Opus",
                    200000,
                    16384,
                ),
                (
                    "Claude Haiku (CLI)",
                    ApiStyle.CLAUDE_CLI,
                    "claude-cli",
                    "haiku",
                    "Anthropic",
                    "Claude Haiku",
                    200000,
                    8192,
                ),
            ],
        ),
        (
            "Local Codex CLI",
            "local://codex",
            [
                ("Codex GPT-5.4 (CLI)", ApiStyle.CODEX_CLI, "codex-cli", "gpt-5.4", "OpenAI", "GPT-5.4", 272000, 16384),
                (
                    "Codex GPT-5.4 Mini (CLI)",
                    ApiStyle.CODEX_CLI,
                    "codex-cli",
                    "gpt-5.4-mini",
                    "OpenAI",
                    "GPT-5.4 Mini",
                    272000,
                    16384,
                ),
                ("Codex o3 (CLI)", ApiStyle.CODEX_CLI, "codex-cli", "o3", "OpenAI", "o3", 200000, 16384),
            ],
        ),
    ]

    existing_profiles = services.profiles.list(include_archived=True)
    for ep_name, base_url, profile_defs in cli_specs:
        ep = existing_endpoints.get(base_url)
        if ep is None:
            ep = services.endpoints.create(name=ep_name, base_url=base_url, region="", auth_style="local_cli")
            existing_endpoints[base_url] = ep
            endpoints_created += 1

        ep_deployments = {p.deployment.lower() for p in existing_profiles if p.endpoint_id == ep.id}
        for prof_name, style, preset_id, deployment, publisher, model_name, ctx_win, max_out in profile_defs:
            if deployment.lower() in ep_deployments:
                continue
            caps = preset_capabilities(preset_id, style, max_out)
            created = services.profiles.create(
                ProfileFields(
                    endpoint_id=ep.id,
                    name=prof_name,
                    api_style=style,
                    route="cli",
                    deployment=deployment,
                    publisher=publisher,
                    model_name=model_name,
                    context_window=ctx_win,
                    max_output_tokens=max_out,
                    preset=preset_id,
                    capabilities=caps,
                    streaming=style is ApiStyle.CODEX_CLI,
                )
            )
            services.pricing.match_and_apply(created.id)
            ep_deployments.add(deployment.lower())
            profiles_created += 1

    return SeedLocalCliOut(
        endpoints_created=endpoints_created,
        profiles_created=profiles_created,
        endpoints=[_out(services, e) for e in services.endpoints.list()],
    )


@router.get("/{endpoint_id}", response_model=EndpointOut)
def get_endpoint(endpoint_id: str, services: ServicesDep) -> EndpointOut:
    return _out(services, services.endpoints.get(endpoint_id))


@router.patch("/{endpoint_id}", response_model=EndpointOut)
def update_endpoint(endpoint_id: str, payload: EndpointIn, services: ServicesDep) -> EndpointOut:
    previous = services.endpoints.get(endpoint_id)
    endpoint, host_changed = services.endpoints.update(endpoint_id, **payload.model_dump())
    if host_changed:
        # Never send a stored key to a different host: the old credential is deleted.
        services.credentials.delete(previous.credential_ref)
    return _out(services, endpoint)


@router.delete("/{endpoint_id}", status_code=204)
def delete_endpoint(endpoint_id: str, services: ServicesDep) -> Response:
    endpoint = services.endpoints.delete(endpoint_id)
    services.credentials.delete(endpoint.credential_ref)
    return Response(status_code=204)


@router.put("/{endpoint_id}/key", response_model=KeyStatus)
def set_key(endpoint_id: str, payload: KeyIn, services: ServicesDep) -> KeyStatus:
    endpoint = services.endpoints.get(endpoint_id)
    services.credentials.set(endpoint.credential_ref, payload.api_key)
    services.endpoints.mark_key_set(endpoint_id, True)
    return KeyStatus(key_configured=True)


@router.delete("/{endpoint_id}/key", response_model=KeyStatus)
def delete_key(endpoint_id: str, services: ServicesDep) -> KeyStatus:
    endpoint = services.endpoints.get(endpoint_id)
    services.credentials.delete(endpoint.credential_ref)
    services.endpoints.mark_key_set(endpoint_id, False)
    return KeyStatus(key_configured=endpoint.auth_style == "local_cli")


@router.post("/{endpoint_id}/verify", response_model=VerifyResult)
async def verify_key(endpoint_id: str, services: ServicesDep) -> VerifyResult:
    """Free metadata call (list models on OpenAI v1 or check local CLI binary); never a paid inference call."""
    endpoint = services.endpoints.get(endpoint_id)
    if endpoint.auth_style == "local_cli" or endpoint.base_url.startswith("local://"):
        ok, status_code, msg = await verify_local_cli(endpoint.base_url)
        return VerifyResult(ok=ok, status=status_code, message=msg)
    key = services.credentials.get(endpoint.credential_ref)
    if not key:
        return VerifyResult(ok=False, status=None, message="No API key is stored for this endpoint.")
    header: dict[str, str] = {"Authorization": f"Bearer {key}"} if endpoint.auth_style == "bearer" else {"api-key": key}
    url = endpoint.base_url.rstrip("/") + "/openai/v1/models"
    try:
        async with make_async_client(timeout=make_timeout(10, 20), url=url) as client:
            response = await client.get(url, headers=header)
    except httpx2.HTTPError as exc:
        return VerifyResult(ok=False, status=None, message=sanitize_error_text(f"Network error: {exc}") or "")
    status: Literal["ok", "bad"] = "ok" if response.status_code == 200 else "bad"
    if 300 <= response.status_code < 400:
        message = redirect_message(response.status_code, response.headers.get("location"))
        return VerifyResult(ok=False, status=response.status_code, message=message)
    messages = {
        200: "The key was accepted (models listed).",
        401: "The key was rejected (HTTP 401).",
        403: "Access denied (HTTP 403).",
        404: "This endpoint does not expose the model list; the key could not be verified this way.",
    }
    message = messages.get(response.status_code, f"Unexpected response (HTTP {response.status_code}).")
    return VerifyResult(ok=status == "ok", status=response.status_code, message=message)
