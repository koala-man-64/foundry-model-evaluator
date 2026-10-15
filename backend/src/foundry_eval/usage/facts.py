"""Normalized usage facts: only what the provider reported, never guessed.

``None`` means "not reported" and is shown as such. It is never treated as zero.
"""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict, Field

from ..types import ApiStyle


class UsageFacts(BaseModel):
    model_config = ConfigDict(frozen=True)

    api_style: ApiStyle
    # Total input tokens (OpenAI: reported; Claude: sum of its three input fields when all are reported).
    input_total: int | None = None
    # Uncached input. Claude reports it directly; for OpenAI it is derived only when the total,
    # cache reads and cache writes are all reported (see ``input_uncached_derived``).
    input_uncached: int | None = None
    input_uncached_derived: bool = False
    cache_read: int | None = None
    cache_write: int | None = None
    cache_write_5m: int | None = None
    cache_write_1h: int | None = None
    # True when the provider sent an incomplete TTL breakdown (which is then ignored).
    cache_write_split_partial: bool = False
    # Billed output tokens. Reasoning/thinking tokens are already included here.
    output_total: int | None = None
    # Display-only subset of output_total.
    reasoning: int | None = None
    provider_total: int | None = None
    service_tier: str | None = None
    # Billing-relevant inconsistencies; any entry makes the cost estimate incomplete.
    anomalies: list[str] = Field(default_factory=list)
    # Informational observations that do not affect billing.
    notes: list[str] = Field(default_factory=list)
    # Non-zero token fields we do not understand (path -> value); make the estimate incomplete.
    unrecognized_nonzero: dict[str, int] = Field(default_factory=dict)
    # Non-zero audio/image tokens (text-only in v1); make the estimate incomplete.
    unsupported_modalities: dict[str, int] = Field(default_factory=dict)
    # Known subset fields kept for display (e.g. text_tokens, prediction tokens).
    informational: dict[str, int] = Field(default_factory=dict)

    @property
    def cache_write_split_reported(self) -> bool:
        return self.cache_write_5m is not None and self.cache_write_1h is not None
