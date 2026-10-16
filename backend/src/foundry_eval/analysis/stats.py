"""Uncertainty for comparisons: 95% intervals across prompts and paired differences against a baseline.

The unit of evidence is the **prompt**. Trials of one prompt are replicates of the same task, so they
are averaged first; counting them as independent samples would make intervals look tighter than the
evidence allows. Intervals are Student-t intervals of the mean across prompts. They need at least
``MIN_PROMPTS`` prompts; below that, a comparison reports that there is not enough data instead of a
number that looks precise. With few prompts the intervals are wide, which is the honest answer.

The verdict of a paired difference is "higher" or "lower" only when the whole interval is on one side
of zero; otherwise it is "cannot_tell". Intervals for several candidates are not corrected for multiple
comparisons, which the documentation says.

When every value is the same (zero variance, for example three straight wins), a t-interval would
collapse to a single point and claim certainty the data cannot give. There is then no interval; a
difference gets a direction only if an exact two-sided sign test supports it (all of at least
``SIGN_TEST_MIN`` prompts on the same side), and otherwise "cannot_tell".
"""

from __future__ import annotations

import math
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from decimal import Decimal, localcontext
from typing import Literal

MIN_PROMPTS = 3
# With n prompts all on one side, the exact two-sided sign test gives p = 2 * 0.5**n; n >= 6 is p < 0.05.
SIGN_TEST_MIN = 6

# Two-sided 95% critical values of Student's t (0.975 quantile) by degrees of freedom. Between
# tabulated values above 30 the next lower entry is used, which gives a slightly wider interval.
_T_975: dict[int, float] = {
    1: 12.706, 2: 4.303, 3: 3.182, 4: 2.776, 5: 2.571, 6: 2.447, 7: 2.365, 8: 2.306, 9: 2.262, 10: 2.228,
    11: 2.201, 12: 2.179, 13: 2.160, 14: 2.145, 15: 2.131, 16: 2.120, 17: 2.110, 18: 2.101, 19: 2.093, 20: 2.086,
    21: 2.080, 22: 2.074, 23: 2.069, 24: 2.064, 25: 2.060, 26: 2.056, 27: 2.052, 28: 2.048, 29: 2.045, 30: 2.042,
    40: 2.021, 60: 2.000, 120: 1.980,
}  # fmt: skip

Verdict = Literal["higher", "lower", "cannot_tell", "insufficient_data"]


def t_critical(df: int) -> float:
    if df < 1:
        raise ValueError("degrees of freedom must be at least 1")
    if df in _T_975:
        return _T_975[df]
    lower = [d for d in _T_975 if d <= df]
    return _T_975[max(lower)]  # above 120 the df=120 value (1.980), slightly wider than the limit 1.960


@dataclass(frozen=True)
class Interval:
    mean: float
    low: float
    high: float
    n: int


@dataclass(frozen=True)
class DecimalInterval:
    mean: Decimal
    low: Decimal
    high: Decimal
    n: int


@dataclass(frozen=True)
class Difference:
    """A paired difference (candidate minus baseline) over the prompts both have."""

    mean: float
    low: float | None
    high: float | None
    n: int
    verdict: Verdict


@dataclass(frozen=True)
class DecimalDifference:
    mean: Decimal
    low: Decimal | None
    high: Decimal | None
    n: int
    verdict: Verdict


def mean_interval(values: list[float], *, floor: float | None = None, ceiling: float | None = None) -> Interval | None:
    """95% interval for the mean, or None below MIN_PROMPTS. Optional bounds clip the interval."""
    n = len(values)
    if n < MIN_PROMPTS:
        return None
    mean = math.fsum(values) / n
    variance = math.fsum((v - mean) ** 2 for v in values) / (n - 1)
    if variance == 0:
        return None  # identical values: a zero-width interval would overstate the evidence
    half = t_critical(n - 1) * math.sqrt(variance / n)
    low, high = mean - half, mean + half
    if floor is not None:
        low = max(low, floor)
    if ceiling is not None:
        high = min(high, ceiling)
    return Interval(mean=mean, low=low, high=high, n=n)


def decimal_mean_interval(values: list[Decimal]) -> DecimalInterval | None:
    """The same interval in exact decimal arithmetic (for money); the lower end never goes below zero."""
    n = len(values)
    if n < MIN_PROMPTS:
        return None
    with localcontext() as ctx:
        ctx.prec = 40
        count = Decimal(n)
        mean = sum(values, Decimal(0)) / count
        variance = sum(((v - mean) ** 2 for v in values), Decimal(0)) / (count - 1)
        if variance == 0:
            return None
        half = Decimal(str(t_critical(n - 1))) * (variance / count).sqrt()
        low = max(mean - half, Decimal(0))
        high = mean + half
    return DecimalInterval(mean=mean, low=low, high=high, n=n)


def _constant_verdict(mean: float | Decimal, n: int) -> Verdict:
    """Every difference is the same: a direction only when an exact sign test supports it."""
    if mean == 0 or n < SIGN_TEST_MIN:
        return "cannot_tell"
    return "higher" if mean > 0 else "lower"


def _verdict(low: float | Decimal | None, high: float | Decimal | None) -> Verdict:
    if low is None or high is None:
        return "insufficient_data"
    if low > 0:
        return "higher"
    if high < 0:
        return "lower"
    return "cannot_tell"


def paired_difference(candidate: Mapping[str, float], baseline: Mapping[str, float]) -> Difference | None:
    """Candidate minus baseline over the prompts both have; None when they share no prompt."""
    prompts = sorted(set(candidate) & set(baseline))
    if not prompts:
        return None
    diffs = [candidate[p] - baseline[p] for p in prompts]
    n = len(diffs)
    mean = math.fsum(diffs) / n
    if n >= MIN_PROMPTS and len(set(diffs)) == 1:
        return Difference(mean=mean, low=None, high=None, n=n, verdict=_constant_verdict(mean, n))
    interval = mean_interval(diffs)
    low = interval.low if interval else None
    high = interval.high if interval else None
    return Difference(mean=mean, low=low, high=high, n=n, verdict=_verdict(low, high))


def decimal_paired_difference(
    candidate: Mapping[str, Decimal], baseline: Mapping[str, Decimal]
) -> DecimalDifference | None:
    prompts = sorted(set(candidate) & set(baseline))
    if not prompts:
        return None
    diffs = [candidate[p] - baseline[p] for p in prompts]
    n = len(diffs)
    with localcontext() as ctx:
        ctx.prec = 40
        mean = sum(diffs, Decimal(0)) / Decimal(n)
        low: Decimal | None = None
        high: Decimal | None = None
        if n >= MIN_PROMPTS and len(set(diffs)) == 1:
            return DecimalDifference(mean=mean, low=None, high=None, n=n, verdict=_constant_verdict(mean, n))
        if n >= MIN_PROMPTS:
            variance = sum(((d - mean) ** 2 for d in diffs), Decimal(0)) / Decimal(n - 1)
            half = Decimal(str(t_critical(n - 1))) * (variance / Decimal(n)).sqrt()
            low, high = mean - half, mean + half
    return DecimalDifference(mean=mean, low=low, high=high, n=n, verdict=_verdict(low, high))


def by_prompt[T, V](
    per_cell: Mapping[tuple[str, int], T], cells: list[tuple[str, int]], value: Callable[[T], V]
) -> dict[str, list[V]]:
    """Group the values of the given cells by prompt (the first element of a cell)."""
    grouped: dict[str, list[V]] = {}
    for cell in cells:
        if cell in per_cell:
            grouped.setdefault(cell[0], []).append(value(per_cell[cell]))
    return grouped


def prompt_means(grouped: Mapping[str, list[float]]) -> dict[str, float]:
    return {prompt: math.fsum(values) / len(values) for prompt, values in grouped.items() if values}


def decimal_prompt_means(grouped: Mapping[str, list[Decimal]]) -> dict[str, Decimal]:
    with localcontext() as ctx:
        ctx.prec = 40
        return {prompt: sum(values, Decimal(0)) / Decimal(len(values)) for prompt, values in grouped.items() if values}
