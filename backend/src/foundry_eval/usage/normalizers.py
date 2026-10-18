"""Usage normalizers for the three supported API styles.

Each normalizer maps the provider's original usage object to :class:`UsageFacts` without
inventing values. Field semantics (verified against the official SDK type definitions):

* OpenAI Responses: ``input_tokens`` includes ``input_tokens_details.cached_tokens`` and
  ``input_tokens_details.cache_write_tokens`` (GPT-5.6+); ``output_tokens`` includes
  ``output_tokens_details.reasoning_tokens``.
* OpenAI Chat Completions: the same with ``prompt_tokens``/``completion_tokens`` and the
  ``*_tokens_details`` objects.
* Claude Messages: ``input_tokens`` is uncached input only; cache reads and writes are reported
  separately (``cache_read_input_tokens``, ``cache_creation_input_tokens`` with an optional
  ``cache_creation`` TTL breakdown); ``output_tokens`` includes thinking tokens, optionally
  decomposed in ``output_tokens_details.thinking_tokens``.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any, Protocol

from ..types import ApiStyle
from .facts import UsageFacts

_MODALITY_FIELDS = ("audio_tokens", "image_tokens")
_ROOT: Any = object()


class UsageNormalizer(Protocol):
    api_style: ApiStyle

    def normalize(self, usage: Any, *, service_tier: str | None = None) -> UsageFacts | None: ...


_MAX_EXACT_FLOAT = 2**53


def _int(value: Any) -> int | None:
    if isinstance(value, bool) or value is None:
        return None
    if isinstance(value, int):
        return value
    if isinstance(value, float) and value.is_integer() and abs(value) <= _MAX_EXACT_FLOAT:
        return int(value)
    return None


class _Collector:
    """Tracks consumed fields so unknown non-zero token counts can be detected."""

    def __init__(self, usage: Mapping[str, Any]) -> None:
        self.usage = usage
        self.anomalies: list[str] = []
        self.notes: list[str] = []
        self.unrecognized: dict[str, int] = {}
        self.modalities: dict[str, int] = {}
        self.informational: dict[str, int] = {}

    def take(self, path: str) -> int | None:
        node: Any = self.usage
        for part in path.split("."):
            if not isinstance(node, Mapping) or part not in node:
                return None
            node = node[part]
        value = _int(node)
        if node is not None and value is None:
            self.anomalies.append(f"{path} is not an integer")
        elif value is not None and value < 0:
            self.anomalies.append(f"{path} is negative ({value})")
            return None
        return value

    def scan_unknown(self, known: Mapping[str, Any], node: Any = _ROOT, prefix: str = "") -> None:
        """Walk the usage object; classify fields not listed in ``known``.

        ``known`` maps field names to either ``True`` (consumed scalar) or a nested mapping.
        """
        if node is _ROOT:
            node = self.usage
        if not isinstance(node, Mapping):
            return
        for key, value in node.items():
            path = f"{prefix}{key}"
            spec = known.get(key)
            if isinstance(spec, Mapping):
                self.scan_unknown(spec, value, path + ".")
                continue
            if spec is not None:
                continue
            if isinstance(value, Mapping):
                self.scan_unknown({}, value, path + ".")
                continue
            if isinstance(value, list):
                # e.g. per-iteration usage blocks: token counts inside are billing-relevant.
                for index, item in enumerate(value):
                    if isinstance(item, Mapping):
                        self.scan_unknown({}, item, f"{path}[{index}].")
                continue
            number = _int(value)
            if number is None:
                if key.endswith("tokens") and value is not None and not isinstance(value, bool):
                    self.anomalies.append(f"{path} has a non-integer value")
                continue
            if number == 0:
                continue
            if key in _MODALITY_FIELDS:
                self.modalities[path] = number
            elif key in ("text_tokens", "accepted_prediction_tokens", "rejected_prediction_tokens"):
                self.informational[path] = number
            elif "token" in key or "request" in key:
                self.unrecognized[path] = number


def _check_reasoning(collector: _Collector, reasoning: int | None, output: int | None) -> None:
    if reasoning is not None and output is not None and reasoning > output:
        collector.notes.append(f"reported reasoning tokens ({reasoning}) exceed output tokens ({output})")


def _openai_facts(
    api_style: ApiStyle,
    usage: Mapping[str, Any],
    service_tier: str | None,
    *,
    input_key: str,
    output_key: str,
    input_details: str,
    output_details: str,
) -> UsageFacts:
    c = _Collector(usage)
    total_in = c.take(input_key)
    output = c.take(output_key)
    provider_total = c.take("total_tokens")
    cached = c.take(f"{input_details}.cached_tokens")
    cache_write = c.take(f"{input_details}.cache_write_tokens")
    reasoning = c.take(f"{output_details}.reasoning_tokens")
    c.scan_unknown(
        {
            input_key: True,
            output_key: True,
            "total_tokens": True,
            input_details: {"cached_tokens": True, "cache_write_tokens": True},
            output_details: {"reasoning_tokens": True},
        }
    )
    if total_in is not None:
        subset = (cached or 0) + (cache_write or 0)
        if subset > total_in:
            c.anomalies.append(f"cache reads + cache writes ({subset}) exceed total input tokens ({total_in})")
    uncached: int | None = None
    if total_in is not None and cached is not None and cache_write is not None and not c.anomalies:
        uncached = total_in - cached - cache_write
    if (
        provider_total is not None
        and total_in is not None
        and output is not None
        and provider_total != total_in + output
    ):
        c.notes.append(f"total_tokens ({provider_total}) differs from input + output ({total_in + output})")
    _check_reasoning(c, reasoning, output)
    return UsageFacts(
        api_style=api_style,
        input_total=total_in,
        input_uncached=uncached,
        input_uncached_derived=uncached is not None,
        cache_read=cached,
        cache_write=cache_write,
        output_total=output,
        reasoning=reasoning,
        provider_total=provider_total,
        service_tier=service_tier,
        anomalies=c.anomalies,
        notes=c.notes,
        unrecognized_nonzero=c.unrecognized,
        unsupported_modalities=c.modalities,
        informational=c.informational,
    )


class ResponsesUsageNormalizer:
    api_style = ApiStyle.OPENAI_RESPONSES

    def normalize(self, usage: Any, *, service_tier: str | None = None) -> UsageFacts | None:
        if not usage:
            return None
        if not isinstance(usage, Mapping):
            return UsageFacts(api_style=self.api_style, anomalies=["usage is not an object"])
        return _openai_facts(
            self.api_style,
            usage,
            service_tier,
            input_key="input_tokens",
            output_key="output_tokens",
            input_details="input_tokens_details",
            output_details="output_tokens_details",
        )


class ChatUsageNormalizer:
    api_style = ApiStyle.OPENAI_CHAT

    def normalize(self, usage: Any, *, service_tier: str | None = None) -> UsageFacts | None:
        if not usage:
            return None
        if not isinstance(usage, Mapping):
            return UsageFacts(api_style=self.api_style, anomalies=["usage is not an object"])
        return _openai_facts(
            self.api_style,
            usage,
            service_tier,
            input_key="prompt_tokens",
            output_key="completion_tokens",
            input_details="prompt_tokens_details",
            output_details="completion_tokens_details",
        )


class ClaudeUsageNormalizer:
    api_style = ApiStyle.ANTHROPIC_MESSAGES

    def normalize(self, usage: Any, *, service_tier: str | None = None) -> UsageFacts | None:
        return _claude_facts(self.api_style, usage, service_tier=service_tier, allow_cli_fields=False)


class ClaudeCliUsageNormalizer:
    api_style = ApiStyle.CLAUDE_CLI

    def normalize(self, usage: Any, *, service_tier: str | None = None) -> UsageFacts | None:
        return _claude_facts(self.api_style, usage, service_tier=service_tier, allow_cli_fields=True)


def _claude_facts(
    api_style: ApiStyle,
    usage: Any,
    *,
    service_tier: str | None = None,
    allow_cli_fields: bool = False,
) -> UsageFacts | None:
    if not usage:
        return None
    if not isinstance(usage, Mapping):
        return UsageFacts(api_style=api_style, anomalies=["usage is not an object"])
    c = _Collector(usage)
    uncached = c.take("input_tokens")
    cache_read = c.take("cache_read_input_tokens")
    cache_write = c.take("cache_creation_input_tokens")
    write_5m = c.take("cache_creation.ephemeral_5m_input_tokens")
    write_1h = c.take("cache_creation.ephemeral_1h_input_tokens")
    output = c.take("output_tokens")
    thinking = c.take("output_tokens_details.thinking_tokens")
    tier = usage.get("service_tier") if isinstance(usage.get("service_tier"), str) else None
    known: dict[str, Any] = {
        "input_tokens": True,
        "cache_read_input_tokens": True,
        "cache_creation_input_tokens": True,
        "cache_creation": {"ephemeral_5m_input_tokens": True, "ephemeral_1h_input_tokens": True},
        "output_tokens": True,
        "output_tokens_details": {"thinking_tokens": True},
        "service_tier": True,
        "inference_geo": True,
        "speed": True,
    }
    if allow_cli_fields:
        known["iterations"] = True
        known["_model_usage"] = True
        known["_requested_model"] = True
        _check_claude_cli_fields(c, usage, uncached=uncached, output=output)

    c.scan_unknown(known)
    # Non-zero ``server_tool_use`` request counts (web search/fetch) are flagged as unrecognized
    # by scan_unknown: tool charges are not modelled in v1.
    partial_split = (write_5m is None) != (write_1h is None)
    if partial_split:
        # A partial breakdown cannot be trusted; treat the split as not reported (and do not
        # infer it from the request either).
        c.notes.append("cache_creation TTL breakdown is incomplete; ignoring it")
        write_5m = write_1h = None
    speed = usage.get("speed")
    if isinstance(speed, str) and speed.strip().lower() not in ("", "standard"):
        c.unrecognized[f"speed={speed}"] = 1
    if write_5m is not None and write_1h is not None:
        if cache_write is None:
            cache_write = write_5m + write_1h
        elif write_5m + write_1h != cache_write:
            c.anomalies.append(
                f"cache_creation breakdown ({write_5m} + {write_1h}) does not match "
                f"cache_creation_input_tokens ({cache_write})"
            )
    total_in: int | None = None
    if uncached is not None and cache_read is not None and cache_write is not None:
        total_in = uncached + cache_read + cache_write
    _check_reasoning(c, thinking, output)
    return UsageFacts(
        api_style=api_style,
        input_total=total_in,
        input_uncached=uncached,
        cache_read=cache_read,
        cache_write=cache_write,
        cache_write_5m=write_5m,
        cache_write_1h=write_1h,
        cache_write_split_partial=partial_split,
        output_total=output,
        reasoning=thinking,
        service_tier=tier or service_tier,
        anomalies=c.anomalies,
        notes=c.notes,
        unrecognized_nonzero=c.unrecognized,
        unsupported_modalities=c.modalities,
        informational=c.informational,
    )


def _check_claude_cli_fields(
    c: _Collector,
    usage: Mapping[str, Any],
    *,
    uncached: int | None,
    output: int | None,
) -> None:
    iterations = usage.get("iterations")
    if isinstance(iterations, list) and iterations:
        iter_in = 0
        iter_out = 0
        has_counts = False
        for idx, item in enumerate(iterations):
            if not isinstance(item, Mapping):
                continue
            i_in = _int(item.get("input_tokens"))
            i_out = _int(item.get("output_tokens"))
            if i_in is not None:
                iter_in += i_in
                has_counts = True
            if i_out is not None:
                iter_out += i_out
                has_counts = True
            for k, v in item.items():
                if k in (
                    "input_tokens",
                    "output_tokens",
                    "cache_read_input_tokens",
                    "cache_creation_input_tokens",
                    "cache_creation",
                    "output_tokens_details",
                    "type",
                ):
                    continue
                num = _int(v)
                if num and ("token" in k or "request" in k):
                    c.unrecognized[f"iterations[{idx}].{k}"] = num
        if has_counts and uncached is not None and iter_in != uncached:
            c.anomalies.append(f"iterations input_tokens sum ({iter_in}) differs from input_tokens ({uncached})")
        if has_counts and output is not None and iter_out != output:
            c.anomalies.append(f"iterations output_tokens sum ({iter_out}) differs from output_tokens ({output})")

    model_usage = usage.get("_model_usage")
    requested = str(usage.get("_requested_model") or "").strip().lower()
    if isinstance(model_usage, Mapping) and len(model_usage) > 1:
        # Identify primary model (matching requested alias/name, else first entry) and flag any
        # secondary model token spend as unrecognized so cost is marked incomplete.
        keys = [str(k) for k in model_usage]
        primary = keys[0]
        if requested:
            for k in keys:
                if requested in k.lower():
                    primary = k
                    break
        for k, entry in model_usage.items():
            if str(k) == primary or not isinstance(entry, Mapping):
                continue
            sec_tokens = 0
            for tk in ("inputTokens", "outputTokens", "cacheReadInputTokens", "cacheCreationInputTokens"):
                val = _int(entry.get(tk))
                if val:
                    sec_tokens += val
            if sec_tokens > 0:
                c.unrecognized[f"modelUsage.{k}.tokens"] = sec_tokens


class CodexCliUsageNormalizer:
    api_style = ApiStyle.CODEX_CLI

    def normalize(self, usage: Any, *, service_tier: str | None = None) -> UsageFacts | None:
        if not usage:
            return None
        if not isinstance(usage, Mapping):
            return UsageFacts(api_style=self.api_style, anomalies=["usage is not an object"])
        c = _Collector(usage)
        total_in = c.take("input_tokens")
        output = c.take("output_tokens")
        provider_total = c.take("total_tokens")
        cached = c.take("cached_input_tokens")
        if cached is None:
            cached = c.take("input_tokens_details.cached_tokens")
        cache_write = c.take("cache_write_input_tokens")
        if cache_write is None:
            cache_write = c.take("input_tokens_details.cache_write_tokens")
        reasoning = c.take("reasoning_output_tokens")
        if reasoning is None:
            reasoning = c.take("output_tokens_details.reasoning_tokens")
        c.scan_unknown(
            {
                "input_tokens": True,
                "output_tokens": True,
                "total_tokens": True,
                "cached_input_tokens": True,
                "cache_write_input_tokens": True,
                "reasoning_output_tokens": True,
                "input_tokens_details": {"cached_tokens": True, "cache_write_tokens": True},
                "output_tokens_details": {"reasoning_tokens": True},
            }
        )
        if total_in is not None:
            subset = (cached or 0) + (cache_write or 0)
            if subset > total_in:
                c.anomalies.append(f"cache reads + cache writes ({subset}) exceed total input tokens ({total_in})")
        uncached: int | None = None
        if total_in is not None and cached is not None and cache_write is not None and not c.anomalies:
            uncached = total_in - cached - cache_write
        if (
            provider_total is not None
            and total_in is not None
            and output is not None
            and provider_total != total_in + output
        ):
            c.notes.append(f"total_tokens ({provider_total}) differs from input + output ({total_in + output})")
        _check_reasoning(c, reasoning, output)
        return UsageFacts(
            api_style=self.api_style,
            input_total=total_in,
            input_uncached=uncached,
            input_uncached_derived=uncached is not None,
            cache_read=cached,
            cache_write=cache_write,
            output_total=output,
            reasoning=reasoning,
            provider_total=provider_total,
            service_tier=service_tier,
            anomalies=c.anomalies,
            notes=c.notes,
            unrecognized_nonzero=c.unrecognized,
            unsupported_modalities=c.modalities,
            informational=c.informational,
        )


NORMALIZERS: dict[ApiStyle, UsageNormalizer] = {
    ApiStyle.OPENAI_RESPONSES: ResponsesUsageNormalizer(),
    ApiStyle.OPENAI_CHAT: ChatUsageNormalizer(),
    ApiStyle.ANTHROPIC_MESSAGES: ClaudeUsageNormalizer(),
    ApiStyle.CLAUDE_CLI: ClaudeCliUsageNormalizer(),
    ApiStyle.CODEX_CLI: CodexCliUsageNormalizer(),
}


def normalize_usage(api_style: ApiStyle, usage: Any, *, service_tier: str | None = None) -> UsageFacts | None:
    return NORMALIZERS[api_style].normalize(usage, service_tier=service_tier)
