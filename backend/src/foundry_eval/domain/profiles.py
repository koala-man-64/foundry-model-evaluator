"""Endpoints (Foundry resources) and model profiles (deployments on an endpoint)."""

from __future__ import annotations

import hashlib
import json
from typing import Literal
from urllib.parse import urlsplit

from pydantic import BaseModel, ConfigDict, Field, field_validator

from ..types import ApiStyle
from .capabilities import Capabilities

AuthStyle = Literal["api-key", "bearer", "local_cli"]
Route = Literal["openai_v1", "foundry_models", "anthropic", "custom", "cli"]
DeploymentType = Literal["global", "data_zone", "regional"]
ProcessingTier = Literal["standard", "priority"]

LOOPBACK_HOSTS = {"127.0.0.1", "localhost", "::1"}
LOCAL_CLI_HOSTS = {"claude", "codex"}
DEFAULT_FOUNDRY_MODELS_API_VERSION = "2024-05-01-preview"


def validate_base_url(value: str) -> str:
    url = value.strip().rstrip("/")
    parts = urlsplit(url)
    if parts.scheme == "local":
        if not parts.hostname or parts.hostname.lower() not in LOCAL_CLI_HOSTS:
            raise ValueError("Local CLI URL must be local://claude or local://codex.")
        if parts.username or parts.password:
            raise ValueError("Endpoint URL must not contain credentials.")
        if parts.query or parts.fragment:
            raise ValueError("Endpoint URL must not contain a query string or fragment.")
        return f"local://{parts.hostname.lower()}{parts.path}"
    if parts.scheme not in ("https", "http") or not parts.hostname:
        raise ValueError("Endpoint URL must be an absolute https:// URL (or local://claude, local://codex).")
    if parts.scheme == "http" and parts.hostname not in LOOPBACK_HOSTS:
        raise ValueError("Plain http:// is allowed only for loopback addresses; use https://.")
    if parts.username or parts.password:
        raise ValueError("Endpoint URL must not contain credentials.")
    if parts.query or parts.fragment:
        raise ValueError("Endpoint URL must not contain a query string or fragment.")
    return url


def host_identity(url: str) -> str:
    parts = urlsplit(url)
    return f"{parts.scheme}://{(parts.hostname or '').lower()}:{parts.port or ''}"


class Endpoint(BaseModel):
    model_config = ConfigDict(frozen=True)

    id: str
    name: str
    base_url: str
    region: str
    auth_style: AuthStyle
    credential_ref: str
    key_set_at: str | None
    created_at: str
    updated_at: str


class ProfileFields(BaseModel):
    """User-editable profile fields (shared by create/update payloads and stored profiles)."""

    model_config = ConfigDict(extra="forbid")

    endpoint_id: str
    name: str = Field(min_length=1, max_length=120)
    api_style: ApiStyle
    route: Route
    custom_path: str | None = None
    api_version: str | None = None
    deployment: str = Field(min_length=1, max_length=200)
    publisher: str = ""
    model_name: str = ""
    model_version: str = ""
    context_window: int | None = Field(default=None, ge=1)
    max_output_tokens: int | None = Field(default=None, ge=1)
    preset: str
    capabilities: Capabilities
    deployment_type: DeploymentType = "global"
    processing_tier: ProcessingTier = "standard"
    pricing_region: str | None = None
    meter_terms: str = ""
    long_context_threshold: int | None = Field(default=None, ge=1)
    streaming: bool = True
    include_usage: bool = True
    timeout_connect_s: float = Field(default=15, gt=0, le=120)
    timeout_read_s: float = Field(default=600, gt=0, le=7200)
    timeout_total_s: float = Field(default=900, gt=0, le=7200)
    notes: str = ""

    @field_validator("custom_path")
    @classmethod
    def _custom_path(cls, value: str | None) -> str | None:
        if value is None or value == "":
            return None
        if not value.startswith("/") or "://" in value or ".." in value:
            raise ValueError("Custom path must start with '/' and stay on the endpoint host.")
        return value


class ModelProfile(ProfileFields):
    model_config = ConfigDict(extra="forbid", frozen=True)

    id: str
    archived: bool = False
    created_at: str
    updated_at: str

    def pricing_identity(self, endpoint: Endpoint, currency: str) -> str:
        """Hash of everything that determines which prices apply to this profile."""
        payload = {
            "api_style": self.api_style.value,
            "host": host_identity(endpoint.base_url),
            "deployment": self.deployment,
            "model_name": self.model_name,
            "model_version": self.model_version,
            "deployment_type": self.deployment_type,
            "processing_tier": self.processing_tier,
            "region": self.pricing_region or endpoint.region,
            "currency": currency,
        }
        return hashlib.sha256(json.dumps(payload, sort_keys=True).encode()).hexdigest()[:24]
