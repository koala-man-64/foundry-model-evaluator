"""Provider-neutral generation settings and their validation against a capability descriptor."""

from __future__ import annotations

import hashlib
import json
from typing import Any

from pydantic import BaseModel, ConfigDict, Field

from .capabilities import SETTING_KEYS, SETTING_KINDS, SETTING_LABELS, Capabilities


class GenerationSettings(BaseModel):
    model_config = ConfigDict(extra="forbid")

    max_output_tokens: int | None = Field(default=None, ge=1)
    temperature: float | None = None
    top_p: float | None = None
    top_k: int | None = Field(default=None, ge=1)
    stop: list[str] | None = None
    seed: int | None = None
    effort: str | None = None
    thinking_mode: str | None = None
    thinking_budget_tokens: int | None = Field(default=None, ge=1)
    thinking_display: str | None = None
    reasoning_summary: str | None = None
    verbosity: str | None = None
    prompt_cache_ttl: str | None = None

    def provided(self) -> dict[str, Any]:
        """Settings explicitly set (``None`` means: omit and use the provider default)."""
        return {k: v for k, v in self.model_dump().items() if v is not None and v != []}

    def canonical_json(self) -> str:
        return json.dumps(self.provided(), sort_keys=True, separators=(",", ":"))

    def settings_hash(self) -> str:
        return hashlib.sha256(self.canonical_json().encode()).hexdigest()[:16]


class SettingIssue(BaseModel):
    setting: str
    message: str


def validate_settings(settings: GenerationSettings, capabilities: Capabilities) -> list[SettingIssue]:
    """Return every problem; an empty list means the settings can be sent as-is."""
    issues: list[SettingIssue] = []
    values = settings.provided()
    for key in SETTING_KEYS:
        spec = capabilities.spec(key)
        label = SETTING_LABELS[key]
        value = values.get(key)
        if value is None:
            if spec.supported and spec.required:
                issues.append(SettingIssue(setting=key, message=f"{label} is required for this model profile."))
            continue
        if not spec.supported:
            issues.append(SettingIssue(setting=key, message=f"{label} is not supported by this model profile."))
            continue
        kind = SETTING_KINDS[key]
        if kind in ("int", "float"):
            number = float(value)
            if spec.minimum is not None and number < spec.minimum:
                issues.append(SettingIssue(setting=key, message=f"{label} must be at least {spec.minimum:g}."))
            if spec.maximum is not None and number > spec.maximum:
                issues.append(SettingIssue(setting=key, message=f"{label} must be at most {spec.maximum:g}."))
        elif kind == "enum":
            if spec.allowed is not None and value not in spec.allowed:
                allowed = ", ".join(spec.allowed)
                issues.append(SettingIssue(setting=key, message=f"{label} must be one of: {allowed}."))
        elif kind == "list":
            if spec.max_items is not None and len(value) > spec.max_items:
                issues.append(SettingIssue(setting=key, message=f"{label} allows at most {spec.max_items} items."))
            if any(not isinstance(item, str) or not item for item in value):
                issues.append(SettingIssue(setting=key, message=f"{label} must be non-empty strings."))
    issues.extend(_cross_field_issues(values))
    return issues


def _cross_field_issues(values: dict[str, Any]) -> list[SettingIssue]:
    issues: list[SettingIssue] = []
    mode = values.get("thinking_mode")
    budget = values.get("thinking_budget_tokens")
    max_out = values.get("max_output_tokens")
    if mode == "enabled" and budget is None:
        issues.append(
            SettingIssue(
                setting="thinking_budget_tokens",
                message="A thinking budget is required when thinking is enabled.",
            )
        )
    if budget is not None and mode != "enabled":
        issues.append(
            SettingIssue(
                setting="thinking_budget_tokens",
                message="A thinking budget applies only when thinking mode is 'enabled'.",
            )
        )
    if budget is not None and max_out is not None and budget >= max_out:
        issues.append(
            SettingIssue(
                setting="thinking_budget_tokens",
                message="The thinking budget must be lower than max output tokens.",
            )
        )
    if values.get("thinking_display") is not None and mode not in ("adaptive", "enabled", None):
        issues.append(
            SettingIssue(setting="thinking_display", message="Thinking display applies only when thinking is on.")
        )
    if mode == "enabled":
        for key in ("temperature", "top_k"):
            if key in values:
                issues.append(
                    SettingIssue(
                        setting=key,
                        message=f"{SETTING_LABELS[key]} cannot be combined with extended thinking.",
                    )
                )
    return issues
