"""Cost projections from measured usage.

Every attempt is re-priced on its own under the scenario (what-if multipliers and cache shares,
original or current prices); the context-length tier is chosen again from the scenario's input.
Only then are costs averaged: prices are never applied to averaged token counts. Only attempts
whose scenario estimate is complete are used, and the number excluded is reported.
"""

from __future__ import annotations

from decimal import ROUND_HALF_UP, Decimal
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

from ..db.database import Database
from ..judging.service import latest_attempts
from ..money import Money, add, div, mul
from ..pricing.calculator import CostContext, estimate_cost
from ..pricing.ratecard import Category, RateCard, RateStatus
from ..pricing.service import PricingService
from ..pricing.snapshots import load_snapshot
from ..repos.endpoints import EndpointRepository, NotFoundError
from ..repos.profiles import ProfileRepository
from ..repos.runs import RunCandidate, RunRepository
from ..types import AttemptStatus
from ..usage.facts import UsageFacts

_ONE = Decimal(1)
PRICE_BASIS_LABELS = {
    "original": "prices: the price snapshot each run was created with",
    "current": "prices: current rates",
}


class WhatIf(BaseModel):
    """Scenario assumptions. They are labelled as assumptions wherever results are shown."""

    model_config = ConfigDict(extra="forbid")

    input_multiplier: Money = Field(default=_ONE)
    output_multiplier: Money = Field(default=_ONE)
    cache_read_share: Money | None = None
    cache_write_share: Money | None = None

    @model_validator(mode="after")
    def _check(self) -> WhatIf:
        for name in ("input_multiplier", "output_multiplier"):
            value = getattr(self, name)
            if not Decimal(0) < value <= Decimal(100):
                raise ValueError(f"{name.replace('_', ' ')} must be greater than 0 and at most 100")
        for name in ("cache_read_share", "cache_write_share"):
            value = getattr(self, name)
            if value is not None and not Decimal(0) <= value <= _ONE:
                raise ValueError(f"{name.replace('_', ' ')} must be between 0 and 1")
        shares = (self.cache_read_share or Decimal(0)) + (self.cache_write_share or Decimal(0))
        if shares > _ONE:
            raise ValueError("cache read and cache write shares together cannot exceed 1")
        return self

    def labels(self) -> list[str]:
        labels: list[str] = []
        if self.input_multiplier != _ONE:
            labels.append(f"input length x{self.input_multiplier.normalize():f}")
        if self.output_multiplier != _ONE:
            labels.append(f"output length x{self.output_multiplier.normalize():f}")
        if self.cache_read_share is not None:
            labels.append(f"cache reads {(self.cache_read_share * 100).normalize():f}% of input")
        if self.cache_write_share is not None:
            labels.append(f"cache writes {(self.cache_write_share * 100).normalize():f}% of input")
        return labels


class ProjectionRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    run_ids: list[str] = Field(min_length=1, max_length=50)
    candidate_keys: list[str] | None = None
    price_basis: Literal["original", "current"] = "original"
    monthly_requests: int = Field(default=10_000, ge=0, le=10**12)
    what_if: WhatIf = Field(default_factory=WhatIf)


class CandidateProjection(BaseModel):
    candidate_key: str
    label: str
    profile_name: str
    attempts_used: int
    attempts_excluded: int
    exclusion_reasons: list[str]
    cost_per_request: Money | None
    cost_per_1k_requests: Money | None
    cost_per_month: Money | None
    status: Literal["complete", "unavailable"]
    assumptions: list[str]
    warnings: list[str]
    custom_rates: bool
    cache_read_applicable: bool
    cache_write_applicable: bool


class ProjectionResult(BaseModel):
    currency: str
    price_basis: Literal["original", "current"]
    monthly_requests: int
    what_if: WhatIf
    assumptions: list[str]
    candidates: list[CandidateProjection]
    notes: list[str]


def _round(value: Decimal) -> int:
    return int(value.quantize(_ONE, rounding=ROUND_HALF_UP))


def _applicable(card: RateCard, categories: tuple[Category, ...]) -> bool:
    tier = card.sorted_tiers()[0]
    return any(c in tier.categories and tier.get(c).status is not RateStatus.NOT_APPLICABLE for c in categories)


def _write_categories(card: RateCard) -> tuple[Category, ...]:
    if card.api_style.is_openai:
        return (Category.CACHE_WRITE,)
    return (Category.CACHE_WRITE_5M, Category.CACHE_WRITE_1H)


def scenario_usage(
    facts: UsageFacts, what_if: WhatIf, *, read_applies: bool = True, write_applies: bool = True
) -> tuple[UsageFacts | None, str | None]:
    """Usage under the scenario, or (None, reason) when the measured counts are insufficient."""
    if facts.output_total is None:
        return None, "output tokens not reported"
    m_in, m_out = what_if.input_multiplier, what_if.output_multiplier

    def scaled(value: int | None, factor: Decimal) -> int | None:
        return None if value is None else _round(Decimal(value) * factor)

    read_share = what_if.cache_read_share if read_applies else None
    write_share = what_if.cache_write_share if write_applies else None
    known_uncached, known_read, known_write = facts.input_uncached, facts.cache_read, facts.cache_write
    read = scaled(known_read, m_in)
    write = scaled(known_write, m_in)
    total: int | None
    if known_uncached is not None and read is not None and write is not None:
        # Scale each part, so the total stays their exact sum.
        total = _round(Decimal(known_uncached) * m_in) + read + write
    else:
        total = scaled(facts.input_total, m_in)
    if total is None:
        return None, "total input tokens not reported"
    if read_share is not None:
        read = _round(Decimal(total) * read_share)
    if write_share is not None:
        write = _round(Decimal(total) * write_share)
    if read is not None and write is not None and read + write > total:
        write = total - read  # rounding of two shares can overshoot by one token
    uncached_out = total - read - write if read is not None and write is not None else None
    write_5m = write_1h = None
    if write is not None and facts.cache_write_split_reported and facts.cache_write:
        # Keep the measured TTL mix when the write volume changes.
        ratio = Decimal(facts.cache_write_5m or 0) / Decimal(facts.cache_write)
        write_5m = _round(Decimal(write) * ratio)
        write_1h = write - write_5m
    elif write == 0:
        write_5m = write_1h = 0
    return (
        UsageFacts(
            api_style=facts.api_style,
            input_total=total,
            input_uncached=uncached_out,
            input_uncached_derived=facts.api_style.is_openai,
            cache_read=read,
            cache_write=write,
            cache_write_5m=write_5m,
            cache_write_1h=write_1h,
            output_total=scaled(facts.output_total, m_out),
            reasoning=scaled(facts.reasoning, m_out),
            service_tier=facts.service_tier,
            anomalies=list(facts.anomalies),
            unrecognized_nonzero=dict(facts.unrecognized_nonzero),
            unsupported_modalities=dict(facts.unsupported_modalities),
        ),
        None,
    )


class ProjectionService:
    def __init__(self, db: Database, pricing: PricingService) -> None:
        self.db = db
        self.pricing = pricing
        self.runs = RunRepository(db)
        self.profiles = ProfileRepository(db)
        self.endpoints = EndpointRepository(db)

    def _card(self, candidate: RunCandidate, basis: str, warnings: list[str]) -> RateCard | None:
        with self.db.connect() as conn:
            original, meta = load_snapshot(conn, candidate.price_snapshot_id)
        if basis == "original":
            return original
        try:
            profile = self.profiles.get(candidate.profile_id)
            endpoint = self.endpoints.get(profile.endpoint_id)
        except NotFoundError:
            warnings.append("The model profile no longer exists, so current prices are unavailable.")
            return None
        card = self.pricing.resolve(profile, endpoint)
        if profile.pricing_identity(endpoint, card.currency) != meta.get("pricing_identity"):
            warnings.append(
                "The profile's pricing identity (deployment, tier, region or endpoint) changed since this run; "
                "current prices may not describe the measured requests."
            )
        return card

    def project(self, request: ProjectionRequest, currency: str) -> ProjectionResult:
        what_if = request.what_if
        # Per candidate key: (run candidate, usage of its succeeded attempts, attempts that cannot be used).
        groups: dict[str, list[tuple[RunCandidate, list[UsageFacts], dict[str, int]]]] = {}
        labels: dict[str, tuple[str, str]] = {}
        order: list[str] = []
        for run_id in dict.fromkeys(request.run_ids):
            self.runs.get(run_id)
            candidates = {c.id: c for c in self.runs.candidates(run_id)}
            usage: dict[str, list[UsageFacts]] = {cid: [] for cid in candidates}
            unusable: dict[str, dict[str, int]] = {cid: {} for cid in candidates}
            for (_prompt, candidate_id, _trial), attempt in latest_attempts(self.runs.attempts(run_id)).items():
                reason = None
                if attempt.status is not AttemptStatus.SUCCEEDED:
                    reason = "did not succeed"
                elif attempt.usage is None:
                    reason = "usage not reported"
                if reason:
                    unusable[candidate_id][reason] = unusable[candidate_id].get(reason, 0) + 1
                elif attempt.usage is not None:
                    usage[candidate_id].append(attempt.usage)
            for candidate_id, candidate in candidates.items():
                if request.candidate_keys and candidate.candidate_key not in request.candidate_keys:
                    continue
                key = candidate.candidate_key
                if key not in groups:
                    groups[key] = []
                    order.append(key)
                labels[key] = (candidate.label, str(candidate.profile_snapshot.get("name", "")))
                groups[key].append((candidate, usage[candidate_id], unusable[candidate_id]))
        results = [self._project_candidate(key, labels[key], groups[key], request, currency) for key in order]
        notes = [
            "Estimated token costs for generation only (judging is excluded), from measured usage of succeeded "
            "requests. Each request is priced on its own, then averaged."
        ]
        return ProjectionResult(
            currency=currency,
            price_basis=request.price_basis,
            monthly_requests=request.monthly_requests,
            what_if=what_if,
            assumptions=[*what_if.labels(), PRICE_BASIS_LABELS[request.price_basis]],
            candidates=results,
            notes=notes,
        )

    def _project_candidate(
        self,
        key: str,
        names: tuple[str, str],
        sources: list[tuple[RunCandidate, list[UsageFacts], dict[str, int]]],
        request: ProjectionRequest,
        currency: str,
    ) -> CandidateProjection:
        costs: list[Decimal] = []
        reasons: dict[str, int] = {}
        assumptions: list[str] = []
        warnings: list[str] = []
        custom = False
        read_applicable = write_applicable = False
        excluded = 0
        for candidate, facts_list, unusable_counts in sources:
            for reason_text, count in unusable_counts.items():
                excluded += count
                reasons[reason_text] = reasons.get(reason_text, 0) + count
            card = self._card(candidate, request.price_basis, warnings)
            price_problem = None
            if card is None:
                price_problem = "prices unavailable"
            elif card.currency != currency:
                price_problem = f"prices in {card.currency}"
            if card is None or price_problem:
                problem = price_problem or "prices unavailable"
                excluded += len(facts_list)
                reasons[problem] = reasons.get(problem, 0) + len(facts_list)
                continue
            read_applies = _applicable(card, (Category.CACHE_READ,))
            write_applies = _applicable(card, _write_categories(card))
            read_applicable = read_applicable or read_applies
            write_applicable = write_applicable or write_applies
            for facts in facts_list:
                scenario, reason = scenario_usage(
                    facts, request.what_if, read_applies=read_applies, write_applies=write_applies
                )
                if scenario is None:
                    excluded += 1
                    reasons[reason or "usage incomplete"] = reasons.get(reason or "usage incomplete", 0) + 1
                    continue
                estimate = estimate_cost(
                    scenario, card, CostContext(requested_cache_ttl=candidate.settings.prompt_cache_ttl)
                )
                if estimate.status != "complete" or estimate.total is None:
                    excluded += 1
                    first = estimate.reasons[0] if estimate.reasons else "estimate incomplete"
                    reasons[first] = reasons.get(first, 0) + 1
                    continue
                costs.append(estimate.total)
                custom = custom or any(line.custom_rate for line in estimate.lines)
                for item in estimate.assumptions:
                    if item not in assumptions:
                        assumptions.append(item)
        if request.what_if.cache_read_share is not None and not read_applicable:
            warnings.append("The cache read share was not applied: cache reads are not billed separately here.")
        if request.what_if.cache_write_share is not None and not write_applicable:
            warnings.append("The cache write share was not applied: cache writes are not billed separately here.")
        mean = div(add(*costs), Decimal(len(costs))) if costs else None
        return CandidateProjection(
            candidate_key=key,
            label=names[0],
            profile_name=names[1],
            attempts_used=len(costs),
            attempts_excluded=excluded,
            exclusion_reasons=[f"{count} excluded: {reason}" for reason, count in reasons.items()],
            cost_per_request=mean,
            cost_per_1k_requests=mul(mean, Decimal(1000)) if mean is not None else None,
            cost_per_month=mul(mean, Decimal(request.monthly_requests)) if mean is not None else None,
            status="complete" if mean is not None else "unavailable",
            assumptions=assumptions,
            warnings=list(dict.fromkeys(warnings)),
            custom_rates=custom,
            cache_read_applicable=read_applicable,
            cache_write_applicable=write_applicable,
        )
