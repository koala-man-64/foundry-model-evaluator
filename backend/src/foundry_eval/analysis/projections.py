"""Cost projections from measured usage.

Every attempt is re-priced on its own under the scenario (what-if multipliers and cache shares,
original or current prices); the context-length tier is chosen again from the scenario's input.
Only then are costs averaged: prices are never applied to averaged token counts. Only attempts
whose scenario estimate is complete are used, and the number excluded is reported.

Scenario input: the measured total is scaled and rounded once. A cache share fixes its category's
part of that total; the rest keeps the measured mix of the other categories. The parts are rounded
with the largest-remainder method, so they always add up to the scenario total.

A batch what-if (a share of requests at an entered price ratio) is then applied to each request's
cost. The spread of the per-request costs is given as nearest-rank percentiles of the same costs the
mean is taken over; a monthly figure at a percentile is the cost if every request cost that much.
"""

from __future__ import annotations

import math
import re
from decimal import ROUND_HALF_UP, Decimal
from typing import Literal, NamedTuple

from pydantic import BaseModel, ConfigDict, Field, model_validator

from ..db.database import Database
from ..judging.service import latest_attempts
from ..money import Money, MoneyInput, add, div, mul
from ..pricing.calculator import CostContext, estimate_cost
from ..pricing.ratecard import Category, RateCard, RateStatus
from ..pricing.service import PricingService
from ..pricing.snapshots import load_snapshot
from ..repos.endpoints import EndpointRepository, NotFoundError
from ..repos.profiles import ProfileRepository
from ..repos.runs import RunCandidate, RunRepository
from ..types import ApiStyle, AttemptStatus
from ..usage.facts import UsageFacts
from .stats import nearest_rank

_ONE = Decimal(1)
PRICE_BASIS_LABELS = {
    "original": "prices: the price snapshot each run was created with",
    "current": "prices: current rates",
}
# Below this many priced requests the percentiles are shown with a warning: the tails rest on a few requests.
SPREAD_MIN_REQUESTS = 30


class WhatIf(BaseModel):
    """Scenario assumptions. They are labelled as assumptions wherever results are shown."""

    model_config = ConfigDict(extra="forbid")

    input_multiplier: MoneyInput = Field(default=_ONE)
    output_multiplier: MoneyInput = Field(default=_ONE)
    cache_read_share: MoneyInput | None = None
    cache_write_share: MoneyInput | None = None
    # Claude: the cache TTL assumed for scenario cache writes when neither the measured usage (TTL
    # split) nor the request (cache TTL setting) says which one applies.
    cache_write_ttl: Literal["5m", "1h"] = "5m"
    # Batch: the share of requests sent as batch jobs, and the batch price as a fraction of the price
    # used here. Both are entered by the user (batch meters are never matched) and go together.
    batch_share: MoneyInput | None = None
    batch_rate_ratio: MoneyInput | None = None

    @model_validator(mode="after")
    def _check(self) -> WhatIf:
        for name in ("input_multiplier", "output_multiplier"):
            value = getattr(self, name)
            if not Decimal(0) < value <= Decimal(100):
                raise ValueError(f"{name.replace('_', ' ')} must be greater than 0 and at most 100")
        for name in ("cache_read_share", "cache_write_share", "batch_share"):
            value = getattr(self, name)
            if value is not None and not Decimal(0) <= value <= _ONE:
                raise ValueError(f"{name.replace('_', ' ')} must be between 0 and 1")
        shares = (self.cache_read_share or Decimal(0)) + (self.cache_write_share or Decimal(0))
        if shares > _ONE:
            raise ValueError("cache read and cache write shares together cannot exceed 1")
        if (self.batch_share is None) != (self.batch_rate_ratio is None):
            raise ValueError("batch share and batch rate ratio must be given together")
        if self.batch_rate_ratio is not None and not Decimal(0) < self.batch_rate_ratio <= _ONE:
            raise ValueError("batch rate ratio must be greater than 0 and at most 1")
        return self

    def batch_cost(self, cost: Decimal) -> Decimal:
        """A request's cost with the batch share at the batch rate ratio and the rest at the full cost."""
        if self.batch_share is None or self.batch_rate_ratio is None:
            return cost
        return add(mul(cost, _ONE - self.batch_share), mul(cost, self.batch_share, self.batch_rate_ratio))

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
        if self.batch_share is not None and self.batch_rate_ratio is not None:
            labels.append(
                f"batch: {(self.batch_share * 100).normalize():f}% of requests at "
                f"x{self.batch_rate_ratio.normalize():f} the cost (entered ratio, not a published batch price)"
            )
        return labels


class ProjectionRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    run_ids: list[str] = Field(min_length=1, max_length=50)
    candidate_keys: list[str] | None = None
    price_basis: Literal["original", "current"] = "original"
    monthly_requests: int = Field(default=10_000, ge=0, le=10**12)
    what_if: WhatIf = Field(default_factory=WhatIf)


class CostSpread(BaseModel):
    """Nearest-rank percentiles of the cost per request, over the requests with complete estimates only."""

    p50: Money
    p90: Money
    p95: Money
    max: Money
    # What a month would cost if every request cost the p50 (or the p95). These are not a forecast range.
    month_if_every_request_p50: Money
    month_if_every_request_p95: Money
    basis: str  # what the percentiles are over, with the number of excluded requests
    sample_warning: str | None  # set below SPREAD_MIN_REQUESTS priced requests


def cost_spread(costs: list[Decimal], monthly_requests: int, excluded: int) -> CostSpread | None:
    """The spread of the per-request costs, or None when no request was priced completely."""
    if not costs:
        return None
    n = len(costs)
    p50, p95 = nearest_rank(costs, 50), nearest_rank(costs, 95)
    requests = f"{n} request{'' if n == 1 else 's'}"
    return CostSpread(
        p50=p50,
        p90=nearest_rank(costs, 90),
        p95=p95,
        max=max(costs),
        month_if_every_request_p50=mul(p50, Decimal(monthly_requests)),
        month_if_every_request_p95=mul(p95, Decimal(monthly_requests)),
        basis=f"nearest-rank over {requests} with complete estimates ({excluded or 'none'} excluded)",
        sample_warning=(
            f"Only {requests} priced: percentiles from fewer than {SPREAD_MIN_REQUESTS} are rough."
            if n < SPREAD_MIN_REQUESTS
            else None
        ),
    )


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
    cost_spread: CostSpread | None
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


def _split(total: int, fractions: dict[str, Decimal]) -> dict[str, int]:
    """Whole numbers proportional to fractions (which sum to 1) that add up exactly to total."""
    exact = {key: Decimal(total) * fraction for key, fraction in fractions.items()}
    parts = {key: math.floor(value) for key, value in exact.items()}
    remaining = total - sum(parts.values())
    # Largest remainders first; ties keep the order of the keys.
    by_remainder = sorted(exact, key=lambda key: exact[key] - parts[key], reverse=True)
    for key in by_remainder[: max(remaining, 0)]:
        parts[key] += 1
    for key in reversed(by_remainder):  # only for fractions that sum to slightly more than 1
        if remaining >= 0:
            break
        if parts[key] > 0:
            parts[key] -= 1
            remaining += 1
    return parts


_NAMES = {"uncached": "uncached input", "read": "cache reads", "write": "cache writes"}


class ScenarioUsage(NamedTuple):
    usage: UsageFacts | None
    reason: str | None  # why the attempt cannot be projected (usage is None)
    assumptions: list[str]  # assumptions this attempt's scenario needed beyond the what-if labels


def scenario_usage(
    facts: UsageFacts,
    what_if: WhatIf,
    *,
    read_applies: bool = True,
    write_applies: bool = True,
    requested_ttl: str | None = None,
) -> ScenarioUsage:
    """Usage under the scenario, or no usage and a reason when the measured counts are insufficient."""
    if facts.output_total is None:
        return ScenarioUsage(None, "output tokens not reported", [])
    if facts.input_total is None:
        return ScenarioUsage(None, "total input tokens not reported", [])
    m_in, m_out = what_if.input_multiplier, what_if.output_multiplier
    total = _round(Decimal(facts.input_total) * m_in)  # rounded once, so the parts cannot cross a tier
    shares: dict[str, Decimal] = {}
    if what_if.cache_read_share is not None and read_applies:
        shares["read"] = what_if.cache_read_share
    if what_if.cache_write_share is not None and write_applies:
        shares["write"] = what_if.cache_write_share
    measured: dict[str, int | None] = {
        key: value if value is None or value >= 0 else None
        for key, value in (("uncached", facts.input_uncached), ("read", facts.cache_read), ("write", facts.cache_write))
    }
    rest = _ONE - sum(shares.values(), Decimal(0))
    unspecified = [key for key in measured if key not in shares]
    assumptions: list[str] = []
    parts: dict[str, int | None]
    if all(measured[key] is not None for key in unspecified):
        # The rest of the input keeps the measured mix of the categories without a share.
        weights = {key: Decimal(measured[key] or 0) for key in unspecified}
        weight_sum = sum(weights.values(), Decimal(0))
        if weight_sum == 0:
            weights, weight_sum = {key: Decimal(key == "uncached") for key in unspecified}, _ONE
        fractions = {key: shares[key] if key in shares else rest * weights[key] / weight_sum for key in measured}
        parts = dict(_split(total, fractions))
        mixed = [key for key in unspecified if key != "uncached" and measured[key]]
        if shares and mixed:
            assumptions.append(
                "the rest of the input keeps its measured mix of uncached input and "
                + " and ".join(_NAMES[key] for key in mixed)
            )
    else:
        # Some measured count is unknown (and so is uncached input). Shares still apply; a known
        # count without a share keeps its measured fraction of the input; unknown counts stay unknown.
        fractions = dict(shares)
        known = {
            key: Decimal(measured[key] or 0) / Decimal(facts.input_total) if facts.input_total else Decimal(0)
            for key in ("read", "write")
            if key not in shares and measured[key] is not None
        }
        room = rest
        known_sum = sum(known.values(), Decimal(0))
        if known_sum > room:
            known = {key: value * room / known_sum for key, value in known.items()}
            assumptions.append(" and ".join(_NAMES[key] for key in known) + " reduced to fit the cache shares")
        fractions.update(known)
        if all(key in fractions for key in ("read", "write")):
            fractions["uncached"] = _ONE - sum(fractions.values(), Decimal(0))
            parts = dict(_split(total, fractions))
        else:
            parts = {key: _round(Decimal(total) * fraction) for key, fraction in fractions.items()}
            parts.update({key: None for key in measured if key not in parts})
    write = parts["write"]
    write_5m = write_1h = None
    if facts.api_style is ApiStyle.ANTHROPIC_MESSAGES and write is not None:
        if write == 0:
            write_5m = write_1h = 0
        elif facts.cache_write_split_reported and facts.cache_write:
            # Keep the measured TTL mix when the write volume changes.
            ttl = _split(
                write,
                {
                    "5m": Decimal(facts.cache_write_5m or 0) / Decimal(facts.cache_write),
                    "1h": Decimal(facts.cache_write_1h or 0) / Decimal(facts.cache_write),
                },
            )
            write_5m, write_1h = ttl["5m"], ttl["1h"]
        elif not facts.cache_write_split_partial and requested_ttl not in ("5m", "1h"):
            # Nothing measured or requested tells the TTL: use the scenario's assumption.
            five = what_if.cache_write_ttl == "5m"
            write_5m, write_1h = (write, 0) if five else (0, write)
            assumptions.append(
                f"cache writes priced at the {'5-minute' if five else '1-hour'} cache rate (assumed TTL)"
            )
        # Otherwise the calculator uses the request's TTL, or treats a partial split as unknown.
    return ScenarioUsage(
        UsageFacts(
            api_style=facts.api_style,
            input_total=total,
            input_uncached=parts["uncached"],
            input_uncached_derived=facts.api_style.is_openai,
            cache_read=parts["read"],
            cache_write=write,
            cache_write_5m=write_5m,
            cache_write_1h=write_1h,
            cache_write_split_partial=facts.cache_write_split_partial,
            output_total=_round(Decimal(facts.output_total) * m_out),
            reasoning=None if facts.reasoning is None else _round(Decimal(facts.reasoning) * m_out),
            service_tier=facts.service_tier,
            anomalies=list(facts.anomalies),
            unrecognized_nonzero=dict(facts.unrecognized_nonzero),
            unsupported_modalities=dict(facts.unsupported_modalities),
        ),
        None,
        assumptions,
    )


_COUNTS = re.compile(r"\s*\(\d[\d,]* tokens?\)|^\d[\d,]* tokens?: | = -?\d[\d,]*")


def _stable_reason(reason: str) -> str:
    """An exclusion reason without the token counts, so the same cause is counted once."""
    return _COUNTS.sub("", reason)


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
                if attempt.status is AttemptStatus.FAILED and attempt.error_type == "no_answer":
                    reason = "billed, but not an answer"
                elif attempt.status is not AttemptStatus.SUCCEEDED:
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
            "requests. Each request is priced on its own, then averaged.",
            "p50, p90 and p95 are nearest-rank percentiles of the cost per request over the same requests as the "
            "average. A month at the p50 or p95 is what it would cost if every request cost that much; it is not "
            "a forecast range.",
        ]
        if what_if.batch_share is not None:
            notes.append(
                "Batch is blended into every request's cost: the batch share at the entered ratio, the rest at the "
                "full cost, so the percentiles are of that blended cost. Batch turnaround, quotas and caching rules "
                "are not modelled; check the ratio against your model's batch price."
            )
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
            requested_ttl = candidate.settings.prompt_cache_ttl
            for facts in facts_list:
                scenario = scenario_usage(
                    facts,
                    request.what_if,
                    read_applies=read_applies,
                    write_applies=write_applies,
                    requested_ttl=requested_ttl,
                )
                if scenario.usage is None:
                    excluded += 1
                    reason = scenario.reason or "usage incomplete"
                    reasons[reason] = reasons.get(reason, 0) + 1
                    continue
                estimate = estimate_cost(scenario.usage, card, CostContext(requested_cache_ttl=requested_ttl))
                if estimate.status != "complete" or estimate.total is None:
                    excluded += 1
                    first = _stable_reason(estimate.reasons[0]) if estimate.reasons else "estimate incomplete"
                    reasons[first] = reasons.get(first, 0) + 1
                    continue
                costs.append(request.what_if.batch_cost(estimate.total))
                custom = custom or any(line.custom_rate for line in estimate.lines)
                for item in [*scenario.assumptions, *estimate.assumptions]:
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
            cost_spread=cost_spread(costs, request.monthly_requests, excluded),
            status="complete" if mean is not None else "unavailable",
            assumptions=assumptions,
            warnings=list(dict.fromkeys(warnings)),
            custom_rates=custom,
            cache_read_applicable=read_applicable,
            cache_write_applicable=write_applicable,
        )
