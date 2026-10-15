"""Totals over many cost estimates (run and suite level)."""

from __future__ import annotations

from decimal import Decimal
from typing import Literal

from pydantic import BaseModel

from ..money import ZERO, Money, add
from ..pricing.calculator import CostEstimate


class CostTotal(BaseModel):
    status: Literal["complete", "incomplete", "pending", "empty"]
    currency: str
    total: Money | None
    lower: Money
    upper: Money | None
    counted: int
    incomplete: int
    pending: int
    # Estimates in another currency cannot be added; they make the total incomplete.
    other_currency: int = 0


def total_costs(costs: list[CostEstimate | None], currency: str = "USD") -> CostTotal:
    lower = ZERO
    upper: Decimal | None = ZERO
    incomplete = pending = counted = other_currency = 0
    for cost in costs:
        if cost is None:
            pending += 1
            continue
        if cost.currency != currency:
            other_currency += 1
            upper = None
            continue
        counted += 1
        lower = add(lower, cost.lower)
        upper = None if upper is None or cost.upper is None else add(upper, cost.upper)
        if not cost.is_complete:
            incomplete += 1
    if counted == 0 and pending == 0 and other_currency == 0:
        status: Literal["complete", "incomplete", "pending", "empty"] = "empty"
    elif pending:
        status = "pending"
    elif incomplete or other_currency:
        status = "incomplete"
    else:
        status = "complete"
    return CostTotal(
        status=status,
        currency=currency,
        total=lower if status in ("complete", "empty") else None,
        lower=lower,
        upper=upper if not pending else None,
        counted=counted,
        incomplete=incomplete,
        pending=pending,
        other_currency=other_currency,
    )


def combine_totals(*totals: CostTotal) -> CostTotal:
    """Add totals computed in the same currency (generation + judging of one run)."""
    currency = totals[0].currency if totals else "USD"
    if any(t.currency != currency for t in totals):
        raise ValueError("totals in different currencies cannot be combined")
    lower = ZERO
    upper: Decimal | None = ZERO
    for item in totals:
        lower = add(lower, item.lower)
        upper = None if upper is None or item.upper is None else add(upper, item.upper)
    statuses = {t.status for t in totals}
    if "pending" in statuses:
        status: Literal["complete", "incomplete", "pending", "empty"] = "pending"
    elif "incomplete" in statuses:
        status = "incomplete"
    elif statuses <= {"empty"}:
        status = "empty"
    else:
        status = "complete"
    return CostTotal(
        status=status,
        currency=currency,
        total=lower if status in ("complete", "empty") else None,
        lower=lower,
        upper=upper,
        counted=sum(t.counted for t in totals),
        incomplete=sum(t.incomplete for t in totals),
        pending=sum(t.pending for t in totals),
        other_currency=sum(t.other_currency for t in totals),
    )
