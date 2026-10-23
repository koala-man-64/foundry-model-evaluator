"""Residual meter matcher for Azure Retail Prices items.

Azure meter names are inconsistent across model families, for example::

    gpt-realtime-2 Text Inp glbl          5.6 terra ShortCo Inp Std Gl
    gpt-realtime-2 Text cached Inp glbl   5.6 terra ShortCo Cd Wr Std Gl
    FW DeepSeek V4.1 Flash cached Inp DZ  MM3.5 Outp glbl

The matcher strips the attribute tokens it understands (direction, cache, deployment type,
processing tier, context tier, modality, units). What remains (the *residual*) must equal the
profile's editable meter terms exactly (ignoring case, spaces and hyphens). Any unrecognized
exclusion token or missing required attribute disqualifies a meter from automatic selection.
A category is resolved automatically only when exactly one meter qualifies; otherwise the
candidates and reasons are returned so the user can pin a meter or enter a manual rate.
Missing a match is acceptable; a wrong match is not.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Literal

from ..types import ApiStyle
from .catalog import CatalogItem
from .ratecard import Category, categories_for
from .units import UnitError, parse_token_unit, unit_in_name

ContextTier = Literal["base", "long", "single"]
MatchStatus = Literal["matched", "ambiguous", "not_found"]

_SPLIT_RE = re.compile(r"[\s_\-/]+")
_UNIT_TOKEN_RE = re.compile(r"^\d+(?:\.\d+)?[km]$")
_INPUT = {"inp", "input"}
_OUTPUT = {"outp", "opt", "output"}
_CACHED = {"cached", "cchd", "cache"}
_WRITE = {"wr", "write", "writes"}
_READ = {"rd", "read", "reads"}
_DEPLOYMENT = {
    "glbl": "global",
    "gl": "global",
    "global": "global",
    "dz": "data_zone",
    "dzone": "data_zone",
    "datazone": "data_zone",
    "rgnl": "regional",
    "regional": "regional",
}
_PROCESSING = {"std": "standard", "standard": "standard", "pp": "priority", "priority": "priority"}
_CONTEXT = {"shortco": "base", "longco": "long"}
_TEXT = {"text", "txt"}
_UNIT_WORDS = {"tokens", "token"}
_EXCLUDED = {
    "batch",
    "ptu",
    "provisioned",
    "ft",
    "finetune",
    "finetuned",
    "fine",
    "tuning",
    "training",
    "hosting",
    "audio",
    "aud",
    "image",
    "img",
    "images",
    "video",
    "hour",
    "hours",
    "hr",
    "dev",
    "developer",
}
_VENDOR_PREFIXES = {"gpt"}


def tokenize(text: str) -> list[str]:
    return [token for token in _SPLIT_RE.split((text or "").lower()) if token]


def normalize_terms(terms: str) -> tuple[str, ...]:
    return tuple(tokenize(terms))


@dataclass
class MeterInfo:
    item: CatalogItem
    residual: tuple[str, ...]
    category: Category | None
    deployment_type: str | None
    processing_tier: str
    context: ContextTier | None
    unit_tokens: int | None
    problems: list[str] = field(default_factory=list)

    @property
    def residual_text(self) -> str:
        return " ".join(self.residual)


def classify(item: CatalogItem, api_style: ApiStyle) -> MeterInfo:
    tokens = tokenize(item.meter_name or item.sku_name)
    residual: list[str] = []
    directions: set[str] = set()
    deployments: set[str] = set()
    processing: set[str] = set()
    contexts: set[str] = set()
    excluded: list[str] = []
    cached = write = read = False
    i = 0
    while i < len(tokens):
        word = tokens[i]
        following = tokens[i + 1] if i + 1 < len(tokens) else None
        if word == "cd" and following in _WRITE:
            write, i = True, i + 2
            continue
        if word == "cd" and following in _READ:
            read, i = True, i + 2
            continue
        if word == "cd" and following in _INPUT:
            # "Cd Inp" / "cd Inp": cached input, which is how live meters name cache reads
            # (for example "5.6 terra ShortCo Cd Inp Std Gl 1M Tokens"; only some say "Cached Inp").
            cached, i = True, i + 2
            directions.add("input")
            continue
        if word == "data" and following == "zone":
            deployments.add("data_zone")
            i += 2
            continue
        if word in _INPUT:
            directions.add("input")
        elif word in _OUTPUT:
            directions.add("output")
        elif word in _CACHED:
            cached = True
        elif word in _WRITE:
            write = True
        elif word in _READ:
            read = True
        elif word in _DEPLOYMENT:
            deployments.add(_DEPLOYMENT[word])
        elif word in _PROCESSING:
            processing.add(_PROCESSING[word])
        elif word in _CONTEXT:
            contexts.add(_CONTEXT[word])
        elif word in _TEXT or word in _UNIT_WORDS:
            pass
        elif _UNIT_TOKEN_RE.match(word) and following in _UNIT_WORDS:
            pass  # "1M Tokens": a unit, not part of the model name (unlike "gpt-4-32k")
        elif word in _EXCLUDED:
            excluded.append(word)
        else:
            residual.append(word)
        i += 1

    problems: list[str] = []
    if excluded:
        problems.append(f"excluded meter type ({', '.join(excluded)})")

    category: Category | None = None
    if write:
        if "output" in directions:
            problems.append("conflicting cache-write and output tokens")
        else:
            category = Category.CACHE_WRITE if api_style.is_openai else None
            if category is None:
                problems.append("cache-write meters are matched only for OpenAI-compatible profiles")
    elif cached or read:
        if "output" in directions:
            problems.append("conflicting cache and output tokens")
        else:
            category = Category.CACHE_READ
    elif directions == {"input"}:
        category = Category.INPUT_UNCACHED
    elif directions == {"output"}:
        category = Category.OUTPUT
    else:
        problems.append("no input/output direction in meter name")

    deployment: str | None = None
    if len(deployments) == 1:
        deployment = next(iter(deployments))
    elif not deployments:
        problems.append("deployment type not stated in meter name")
    else:
        problems.append("conflicting deployment types in meter name")

    tier = "standard"
    if len(processing) == 1:
        tier = next(iter(processing))
    elif len(processing) > 1:
        problems.append("conflicting processing tiers in meter name")

    context: ContextTier | None = "single"
    if len(contexts) == 1:
        context = "base" if "base" in contexts else "long"
    elif len(contexts) > 1:
        context = None
        problems.append("conflicting context tiers in meter name")

    unit_tokens: int | None = None
    try:
        unit_tokens = parse_token_unit(item.unit_of_measure)
    except UnitError:
        problems.append(f"unsupported unit of measure {item.unit_of_measure!r}")
    named = unit_in_name(item.meter_name) or unit_in_name(item.sku_name)
    if unit_tokens is not None and named is not None and named != unit_tokens:
        problems.append(f"unit in name ({named} tokens) conflicts with unitOfMeasure ({item.unit_of_measure})")

    return MeterInfo(
        item=item,
        residual=tuple(residual),
        category=category,
        deployment_type=deployment,
        processing_tier=tier,
        context=context,
        unit_tokens=unit_tokens,
        problems=problems,
    )


def residual_matches(residual: tuple[str, ...], terms: tuple[str, ...]) -> bool:
    if not terms or not residual:
        return False
    if residual == terms:
        return True
    return len(terms) >= 2 and terms[0] in _VENDOR_PREFIXES and residual == terms[1:]


@dataclass
class Candidate:
    info: MeterInfo
    eligible: bool
    reasons: list[str]


@dataclass
class CategoryMatch:
    category: Category
    context: Literal["base", "long"]
    status: MatchStatus
    chosen: Candidate | None
    candidates: list[Candidate]
    message: str


@dataclass
class MatchResult:
    matches: list[CategoryMatch]
    notes: list[str]

    def get(self, category: Category, context: str = "base") -> CategoryMatch | None:
        return next((m for m in self.matches if m.category is category and m.context == context), None)


def match_meters(
    items: list[CatalogItem],
    *,
    api_style: ApiStyle,
    meter_terms: str,
    deployment_type: str,
    processing_tier: str,
    categories: list[Category] | None = None,
) -> MatchResult:
    """Match one profile's billing categories against catalog items for its region/currency."""
    terms = normalize_terms(meter_terms)
    notes: list[str] = []
    if not terms:
        notes.append("Set the meter terms for this profile (for example '5.6 terra' or 'gpt-realtime-2').")
    wanted = categories or list(categories_for(api_style))
    infos = [classify(item, api_style) for item in items]
    relevant = [info for info in infos if residual_matches(info.residual, terms)]

    meter_tiers: dict[str, int] = {}
    for info in relevant:
        meter_tiers[info.item.key] = meter_tiers.get(info.item.key, 0) + 1

    def evaluate(info: MeterInfo) -> Candidate:
        reasons = list(info.problems)
        if info.item.tier_min_units != 0:
            reasons.append("meter has a nonzero minimum volume tier (tierMinimumUnits)")
        if info.deployment_type is not None and info.deployment_type != deployment_type:
            reasons.append(f"deployment type is {info.deployment_type}, profile uses {deployment_type}")
        if info.processing_tier != processing_tier:
            reasons.append(f"processing tier is {info.processing_tier}, profile uses {processing_tier}")
        if meter_tiers.get(info.item.key, 0) > 1:
            reasons.append("meter has several volume tiers (tierMinimumUnits)")
        return Candidate(info=info, eligible=not reasons, reasons=reasons)

    candidates = [evaluate(info) for info in relevant]
    has_long = any(c.eligible and c.info.context == "long" for c in candidates)
    matches: list[CategoryMatch] = []
    contexts: list[Literal["base", "long"]] = ["base", "long"] if has_long else ["base"]
    for context in contexts:
        for category in wanted:
            in_category = [c for c in candidates if c.info.category is category]
            if context == "base":
                fitting = [c for c in in_category if c.eligible and c.info.context in ("base", "single")]
            else:
                fitting = [c for c in in_category if c.eligible and c.info.context == "long"]
            ranked = sorted(in_category, key=lambda c: (not c.eligible, c.info.item.meter_name))
            if len(fitting) == 1:
                chosen = fitting[0]
                matches.append(
                    CategoryMatch(
                        category,
                        context,
                        "matched",
                        chosen,
                        ranked,
                        f"Matched '{chosen.info.item.meter_name}'.",
                    )
                )
            elif not fitting:
                message = "No unambiguous Azure meter found."
                if not in_category and terms:
                    message = "No Azure meter with these meter terms for this category."
                matches.append(CategoryMatch(category, context, "not_found", None, ranked, message))
            else:
                names = ", ".join(sorted({c.info.item.meter_name for c in fitting}))
                matches.append(
                    CategoryMatch(category, context, "ambiguous", None, ranked, f"Several meters qualify: {names}.")
                )
    if has_long:
        notes.append(
            "Long-context (LongCo) meters exist for this model; they apply only when the profile's "
            "long-context threshold is set."
        )
    return MatchResult(matches=matches, notes=notes)


def search_catalog(items: list[CatalogItem], query: str, api_style: ApiStyle, limit: int = 200) -> list[MeterInfo]:
    """Meters whose name contains every query token (for the pricing UI)."""
    wanted = tokenize(query)
    found: list[MeterInfo] = []
    for item in items:
        tokens = tokenize(f"{item.meter_name} {item.sku_name} {item.product_name}")
        if all(any(w in t for t in tokens) for w in wanted):
            found.append(classify(item, api_style))
    found.sort(key=lambda info: (info.residual_text, info.item.meter_name))
    return found[:limit]
