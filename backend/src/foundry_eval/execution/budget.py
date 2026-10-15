"""Run budget guard: reserve a provable worst-case cost before each paid call."""

from __future__ import annotations

import json
import math
import threading
from dataclasses import dataclass, field
from decimal import Decimal
from typing import Any

from ..money import ZERO, add, mul
from ..pricing.calculator import CostEstimate
from ..pricing.ratecard import Category, RateCard, RateStatus

# Every token covers at least one UTF-8 byte of text, so the byte count bounds the token count.
# Chat templates add a small number of framing tokens on top.
FRAMING_TOKENS = 256
_INPUT_CATEGORIES = (
    Category.INPUT_UNCACHED,
    Category.CACHE_READ,
    Category.CACHE_WRITE,
    Category.CACHE_WRITE_5M,
    Category.CACHE_WRITE_1H,
)


def input_token_upper_bound(*texts: str) -> int:
    return sum(len(t.encode("utf-8")) for t in texts) + FRAMING_TOKENS


def body_input_upper_bound(body: dict[str, Any]) -> int:
    """Upper bound on the input tokens of a request body: its JSON bytes (schema included) plus framing."""
    return input_token_upper_bound(json.dumps(body, ensure_ascii=False))


def estimate_input_tokens(*texts: str) -> int:
    """Heuristic (not a bound): about three UTF-8 bytes per token, plus framing."""
    return math.ceil(sum(len(t.encode("utf-8")) for t in texts) / 3) + 32


def worst_case_cost(
    card: RateCard, input_upper: int, max_output_tokens: int | None
) -> tuple[Decimal | None, str | None]:
    """Upper bound on one call's cost, or (None, reason) when it cannot be bounded."""
    if max_output_tokens is None:
        return None, "max output tokens must be set to bound the cost of a call"
    worst: Decimal | None = None
    for tier in card.tiers:
        if tier.above_input_tokens and input_upper <= tier.above_input_tokens:
            continue  # this tier cannot apply to a request this small
        input_rates: list[Decimal] = []
        for category in _INPUT_CATEGORIES:
            entry = tier.categories.get(category)
            if entry is None or entry.status is RateStatus.NOT_APPLICABLE:
                continue
            if entry.status is RateStatus.UNKNOWN or entry.rate is None:
                if category is Category.INPUT_UNCACHED:
                    return None, "the uncached input rate is unknown"
                return None, f"the {category.value} rate is unknown"
            input_rates.append(entry.rate.cost(1))
        output = tier.categories.get(Category.OUTPUT)
        if output is None or output.status is not RateStatus.PRICED or output.rate is None:
            return None, "the output rate is unknown"
        if not input_rates:
            return None, "the uncached input rate is unknown"
        cost = add(mul(Decimal(input_upper), max(input_rates)), output.rate.cost(max_output_tokens))
        worst = cost if worst is None else max(worst, cost)
    if worst is None:
        return None, "no applicable price tier"
    return worst, None


@dataclass
class BudgetState:
    limit: Decimal
    committed: Decimal = ZERO
    reserved: dict[str, Decimal] = field(default_factory=dict)
    _lock: threading.Lock = field(default_factory=threading.Lock)

    def reserve(self, attempt_id: str, worst: Decimal) -> bool:
        with self._lock:
            pending = add(self.committed, *self.reserved.values(), worst)
            if pending > self.limit:
                return False
            self.reserved[attempt_id] = worst
            return True

    def settle(self, attempt_id: str, cost: CostEstimate) -> None:
        with self._lock:
            worst = self.reserved.pop(attempt_id, ZERO)
            if cost.status == "not_sent":
                return
            actual = cost.upper if cost.upper is not None else worst
            self.committed = add(self.committed, actual)

    def release(self, attempt_id: str) -> None:
        with self._lock:
            self.reserved.pop(attempt_id, None)

    def commit_reserved(self, attempt_id: str) -> None:
        """The call was sent but its cost is unknown: count the whole reservation as spent."""
        with self._lock:
            worst = self.reserved.pop(attempt_id, None)
            if worst is not None:
                self.committed = add(self.committed, worst)
