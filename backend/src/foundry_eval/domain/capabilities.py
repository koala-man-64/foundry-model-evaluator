"""Capability descriptors: which generation settings a model profile supports.

The UI renders controls from these descriptors and disables unsupported ones. The backend
rejects any request that sets an unsupported or out-of-range value; nothing is silently dropped.
"""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

SettingKind = Literal["int", "float", "enum", "list"]

SETTING_KEYS: tuple[str, ...] = (
    "max_output_tokens",
    "temperature",
    "top_p",
    "top_k",
    "stop",
    "seed",
    "effort",
    "thinking_mode",
    "thinking_budget_tokens",
    "thinking_display",
    "reasoning_summary",
    "verbosity",
    "prompt_cache_ttl",
)

SETTING_KINDS: dict[str, SettingKind] = {
    "max_output_tokens": "int",
    "temperature": "float",
    "top_p": "float",
    "top_k": "int",
    "stop": "list",
    "seed": "int",
    "effort": "enum",
    "thinking_mode": "enum",
    "thinking_budget_tokens": "int",
    "thinking_display": "enum",
    "reasoning_summary": "enum",
    "verbosity": "enum",
    "prompt_cache_ttl": "enum",
}

SETTING_LABELS: dict[str, str] = {
    "max_output_tokens": "Max output tokens",
    "temperature": "Temperature",
    "top_p": "Top P",
    "top_k": "Top K",
    "stop": "Stop sequences",
    "seed": "Seed",
    "effort": "Reasoning effort",
    "thinking_mode": "Thinking mode",
    "thinking_budget_tokens": "Thinking budget tokens",
    "thinking_display": "Thinking display",
    "reasoning_summary": "Reasoning summary",
    "verbosity": "Verbosity",
    "prompt_cache_ttl": "Prompt cache TTL",
}


class SettingSpec(BaseModel):
    model_config = ConfigDict(extra="forbid")

    supported: bool = False
    minimum: float | None = None
    maximum: float | None = None
    allowed: list[str] | None = None
    max_items: int | None = None
    required: bool = False
    provider_default: str | None = None
    note: str | None = None


class Capabilities(BaseModel):
    model_config = ConfigDict(extra="forbid")

    settings: dict[str, SettingSpec] = Field(default_factory=dict)
    # Provider parameter used for the output limit.
    max_output_param: Literal["max_output_tokens", "max_completion_tokens", "max_tokens"] = "max_output_tokens"
    # Chat Completions role used for system instructions.
    system_role: Literal["system", "developer"] = "system"
    structured_output: bool = False

    def spec(self, key: str) -> SettingSpec:
        return self.settings.get(key) or SettingSpec()
