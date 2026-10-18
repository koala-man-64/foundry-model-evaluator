"""Editable capability and pricing presets by model family.

Presets are starting points only: model capabilities change over time, so every value can be
edited per profile. Notes point out the settings most likely to need adjustment.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from ..pricing.ratecard import Category
from ..types import ApiStyle
from .capabilities import Capabilities, SettingSpec

_SAMPLING_NOTE = "Rejected by most reasoning models; enable only if your model accepts it."
_CLAUDE_SAMPLING_NOTE = "Rejected by Claude Opus 4.7+ and Claude 5 models; Claude 4.6 and older accept it."


@dataclass(frozen=True)
class Preset:
    id: str
    label: str
    description: str
    api_styles: tuple[ApiStyle, ...]
    # Default rate source per category: "azure_auto", "manual" or "not_applicable".
    rate_defaults: dict[Category, str] = field(default_factory=dict)


PRESETS: dict[str, Preset] = {
    p.id: p
    for p in (
        Preset(
            id="openai-reasoning",
            label="OpenAI reasoning (GPT-5 to 5.5, o-series)",
            description="Reasoning effort, verbosity and summaries; no sampling controls; no cache-write charges.",
            api_styles=(ApiStyle.OPENAI_RESPONSES, ApiStyle.OPENAI_CHAT),
            rate_defaults={
                Category.INPUT_UNCACHED: "azure_auto",
                Category.CACHE_READ: "azure_auto",
                Category.CACHE_WRITE: "not_applicable",
                Category.OUTPUT: "azure_auto",
            },
        ),
        Preset(
            id="openai-reasoning-cache-write",
            label="OpenAI reasoning with cache-write charges (GPT-5.6 and later)",
            description="As above, but cache writes are billed separately (Azure 'Cd Wr' meters).",
            api_styles=(ApiStyle.OPENAI_RESPONSES, ApiStyle.OPENAI_CHAT),
            rate_defaults={
                Category.INPUT_UNCACHED: "azure_auto",
                Category.CACHE_READ: "azure_auto",
                Category.CACHE_WRITE: "azure_auto",
                Category.OUTPUT: "azure_auto",
            },
        ),
        Preset(
            id="openai-standard",
            label="OpenAI standard (GPT-4.1, GPT-4o)",
            description="Temperature, top P, stop sequences and seed; no reasoning controls.",
            api_styles=(ApiStyle.OPENAI_RESPONSES, ApiStyle.OPENAI_CHAT),
            rate_defaults={
                Category.INPUT_UNCACHED: "azure_auto",
                Category.CACHE_READ: "azure_auto",
                Category.CACHE_WRITE: "not_applicable",
                Category.OUTPUT: "azure_auto",
            },
        ),
        Preset(
            id="claude-adaptive",
            label="Claude adaptive thinking (Claude 4.6 and later, Claude 5 family)",
            description="Adaptive thinking with effort levels and prompt caching. Rates are entered manually.",
            api_styles=(ApiStyle.ANTHROPIC_MESSAGES,),
            rate_defaults={
                Category.INPUT_UNCACHED: "manual",
                Category.CACHE_READ: "manual",
                Category.CACHE_WRITE_5M: "manual",
                Category.CACHE_WRITE_1H: "manual",
                Category.OUTPUT: "manual",
            },
        ),
        Preset(
            id="claude-legacy",
            label="Claude budget thinking (Claude Haiku 4.5, Sonnet 4.5 and older)",
            description="Extended thinking with a token budget, plus sampling controls. Rates are entered manually.",
            api_styles=(ApiStyle.ANTHROPIC_MESSAGES,),
            rate_defaults={
                Category.INPUT_UNCACHED: "manual",
                Category.CACHE_READ: "manual",
                Category.CACHE_WRITE_5M: "manual",
                Category.CACHE_WRITE_1H: "manual",
                Category.OUTPUT: "manual",
            },
        ),
        Preset(
            id="generic-chat",
            label="Other Foundry chat models (DeepSeek, Llama, Mistral, …)",
            description="Chat Completions with sampling controls; no prompt-cache pricing by default.",
            api_styles=(ApiStyle.OPENAI_CHAT,),
            rate_defaults={
                Category.INPUT_UNCACHED: "azure_auto",
                Category.CACHE_READ: "not_applicable",
                Category.CACHE_WRITE: "not_applicable",
                Category.OUTPUT: "azure_auto",
            },
        ),
        Preset(
            id="claude-cli",
            label="Claude Code CLI (local claude -p)",
            description="Local Claude CLI with model and reasoning effort flags (--model, --effort).",
            api_styles=(ApiStyle.CLAUDE_CLI,),
            rate_defaults={
                Category.INPUT_UNCACHED: "manual",
                Category.CACHE_READ: "manual",
                Category.CACHE_WRITE_5M: "manual",
                Category.CACHE_WRITE_1H: "manual",
                Category.OUTPUT: "manual",
            },
        ),
        Preset(
            id="codex-cli",
            label="Codex CLI (local codex exec)",
            description="Local Codex CLI with model, reasoning effort, and summary config (-m, -c).",
            api_styles=(ApiStyle.CODEX_CLI,),
            rate_defaults={
                Category.INPUT_UNCACHED: "azure_auto",
                Category.CACHE_READ: "azure_auto",
                Category.CACHE_WRITE: "not_applicable",
                Category.OUTPUT: "azure_auto",
            },
        ),
    )
}


def preset_capabilities(preset_id: str, api_style: ApiStyle, max_output: int | None = None) -> Capabilities:
    preset = PRESETS[preset_id]
    if api_style not in preset.api_styles:
        raise ValueError(f"Preset {preset.label!r} does not support the {api_style.value} API.")
    max_tokens = float(max_output) if max_output else None
    output = SettingSpec(supported=True, minimum=1, maximum=max_tokens)
    unsupported_sampling = SettingSpec(supported=False, note=_SAMPLING_NOTE)
    s: dict[str, SettingSpec] = {}
    if preset_id.startswith("openai-reasoning"):
        s = {
            "max_output_tokens": output,
            "effort": SettingSpec(
                supported=True,
                allowed=["low", "medium", "high"],
                provider_default="medium",
                note="Add 'minimal', 'none' or 'xhigh' if your model supports them.",
            ),
            "verbosity": SettingSpec(supported=True, allowed=["low", "medium", "high"], provider_default="medium"),
            "temperature": unsupported_sampling,
            "top_p": unsupported_sampling,
        }
        if api_style is ApiStyle.OPENAI_RESPONSES:
            s["reasoning_summary"] = SettingSpec(supported=True, allowed=["auto", "concise", "detailed"])
        return Capabilities(
            settings=s,
            max_output_param="max_output_tokens" if api_style is ApiStyle.OPENAI_RESPONSES else "max_completion_tokens",
            system_role="developer",
            structured_output=True,
        )
    if preset_id == "openai-standard":
        s = {
            "max_output_tokens": output,
            "temperature": SettingSpec(supported=True, minimum=0, maximum=2, provider_default="1"),
            "top_p": SettingSpec(supported=True, minimum=0, maximum=1, provider_default="1"),
        }
        if api_style is ApiStyle.OPENAI_CHAT:
            s["stop"] = SettingSpec(supported=True, max_items=4)
            s["seed"] = SettingSpec(supported=True)
        return Capabilities(
            settings=s,
            max_output_param="max_output_tokens" if api_style is ApiStyle.OPENAI_RESPONSES else "max_completion_tokens",
            system_role="system",
            structured_output=True,
        )
    if preset_id == "claude-adaptive":
        claude_output = SettingSpec(supported=True, required=True, minimum=1, maximum=max_tokens or 128000)
        s = {
            "max_output_tokens": claude_output,
            "thinking_mode": SettingSpec(
                supported=True,
                allowed=["adaptive"],
                provider_default="adaptive on Claude 5 models",
                note="Add 'disabled' (Claude 4.6) or 'between_tools' (newer Claude models) if your model accepts it.",
            ),
            "effort": SettingSpec(
                supported=True,
                allowed=["low", "medium", "high", "xhigh", "max"],
                provider_default="high (some Claude 5 models default to medium)",
            ),
            "thinking_display": SettingSpec(
                supported=True, allowed=["summarized", "omitted"], provider_default="omitted"
            ),
            "prompt_cache_ttl": SettingSpec(supported=True, allowed=["5m", "1h"]),
            "stop": SettingSpec(supported=True, max_items=8),
            "temperature": SettingSpec(supported=False, minimum=0, maximum=1, note=_CLAUDE_SAMPLING_NOTE),
            "top_p": SettingSpec(supported=False, minimum=0, maximum=1, note=_CLAUDE_SAMPLING_NOTE),
            "top_k": SettingSpec(supported=False, minimum=1, note=_CLAUDE_SAMPLING_NOTE),
        }
        return Capabilities(settings=s, max_output_param="max_tokens", structured_output=True)
    if preset_id == "claude-legacy":
        claude_output = SettingSpec(supported=True, required=True, minimum=1, maximum=max_tokens or 64000)
        s = {
            "max_output_tokens": claude_output,
            "thinking_mode": SettingSpec(supported=True, allowed=["enabled", "disabled"], provider_default="disabled"),
            "thinking_budget_tokens": SettingSpec(
                supported=True, minimum=1024, note="Must be lower than max output tokens."
            ),
            "temperature": SettingSpec(supported=True, minimum=0, maximum=1, provider_default="1"),
            "top_p": SettingSpec(supported=True, minimum=0, maximum=1),
            "top_k": SettingSpec(supported=True, minimum=1),
            "stop": SettingSpec(supported=True, max_items=8),
            "prompt_cache_ttl": SettingSpec(supported=True, allowed=["5m", "1h"]),
        }
        return Capabilities(settings=s, max_output_param="max_tokens", structured_output=False)
    if preset_id == "claude-cli":
        cli_note = "Not exposed as a CLI flag by claude -p; set Max output tokens on the profile for budget caps."
        s = {
            "max_output_tokens": SettingSpec(supported=False, note=cli_note),
            "effort": SettingSpec(
                supported=True,
                allowed=["low", "medium", "high", "xhigh", "max"],
                provider_default="medium",
                note="Passed via --effort <level>.",
            ),
            "temperature": SettingSpec(supported=False, note="Not exposed as a CLI flag by claude -p."),
            "top_p": SettingSpec(supported=False, note="Not exposed as a CLI flag by claude -p."),
            "top_k": SettingSpec(supported=False, note="Not exposed as a CLI flag by claude -p."),
        }
        return Capabilities(settings=s, max_output_param="max_tokens", structured_output=True)
    if preset_id == "codex-cli":
        cli_note = "Not exposed as a CLI flag by codex exec; set Max output tokens on the profile for budget caps."
        s = {
            "max_output_tokens": SettingSpec(supported=False, note=cli_note),
            "effort": SettingSpec(
                supported=True,
                allowed=["minimal", "low", "medium", "high", "xhigh", "max"],
                provider_default="medium",
                note="Passed via -c model_reasoning_effort=<level>.",
            ),
            "reasoning_summary": SettingSpec(
                supported=True,
                allowed=["auto", "concise", "detailed", "none"],
                provider_default="auto",
                note="Passed via -c model_reasoning_summary=<summary>.",
            ),
            "temperature": SettingSpec(supported=False, note="Not exposed as a CLI flag by codex exec."),
            "top_p": SettingSpec(supported=False, note="Not exposed as a CLI flag by codex exec."),
        }
        return Capabilities(
            settings=s, max_output_param="max_output_tokens", system_role="developer", structured_output=True
        )
    # generic-chat
    s = {
        "max_output_tokens": output,
        "temperature": SettingSpec(supported=True, minimum=0, maximum=2),
        "top_p": SettingSpec(supported=True, minimum=0, maximum=1),
        "stop": SettingSpec(supported=True, max_items=4),
        "seed": SettingSpec(supported=True),
    }
    return Capabilities(settings=s, max_output_param="max_tokens", system_role="system", structured_output=False)
