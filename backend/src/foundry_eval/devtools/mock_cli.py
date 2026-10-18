"""Deterministic local CLI simulator (`claude` and `codex`) for unit, integration, and E2E tests.

Invokable as:
  python -m foundry_eval.devtools.mock_cli claude [args...]
  python -m foundry_eval.devtools.mock_cli codex [args...]

Supports magic tokens in the stdin prompt:
  - ``fail-500``: simulate a CLI execution error (non-zero exit)
  - ``tool-call``: simulate an attempted tool call (`FinishReason.TOOL_CALL`)
  - ``empty-answer``: return empty output text with non-zero billed usage
  - ``multi-model``: (claude) include secondary background Haiku token usage in `modelUsage`
"""

from __future__ import annotations

import json
import re
import sys
from pathlib import Path
from typing import Any


def main(argv: list[str] | None = None) -> int:
    args = list(sys.argv[1:] if argv is None else argv)
    if not args:
        sys.stderr.write("Usage: mock_cli <claude|codex> [args...]\n")
        return 2
    kind = args[0].lower()
    rest = args[1:]
    if "--version" in rest or "-V" in rest:
        if kind == "claude":
            sys.stdout.write("2.1.112 (Claude Code mock)\n")
        else:
            sys.stdout.write("codex-cli 0.121.0 (mock)\n")
        return 0
    if kind == "claude":
        return _run_claude(rest)
    if kind == "codex":
        return _run_codex(rest)
    sys.stderr.write(f"Unknown mock CLI kind: {kind}\n")
    return 2


def _build_judge_payload(schema: dict[str, Any], prompt_text: str) -> dict[str, Any]:
    """Construct a valid structured judge output matching ``judge_schema``."""
    props = schema.get("properties") or {}
    responses_spec = ((props.get("responses") or {}).get("items") or {}).get("properties") or {}
    labels: list[str] = list((responses_spec.get("label") or {}).get("enum") or ["A", "B"])
    criteria_items = ((responses_spec.get("criteria") or {}).get("items") or {}).get("properties") or {}
    criteria_keys: list[str] = list((criteria_items.get("key") or {}).get("enum") or [
        "instruction_following",
        "correctness",
        "completeness",
        "clarity",
    ])
    responses_out: list[dict[str, Any]] = []
    for idx, label in enumerate(labels):
        values = [5 - ((idx + offset) % 3) for offset in range(len(criteria_keys))]
        scores = [
            {"key": key, "score": val, "explanation": f"{key} assessment for {label}."}
            for key, val in zip(criteria_keys, values, strict=True)
        ]
        overall = round(sum(values) / max(1, len(values)), 2)
        responses_out.append(
            {
                "label": label,
                "criteria": scores,
                "overall": overall,
                "explanation": f"Summary for {label}.",
            }
        )
    best = max(responses_out, key=lambda r: r["overall"])
    tied = [r for r in responses_out if r["overall"] == best["overall"]]
    return {
        "responses": responses_out,
        "preferred": "tie" if len(tied) > 1 else best["label"],
        "preference_explanation": f"Mock CLI judge preference ({len(prompt_text)} chars judged).",
    }



def _run_claude(args: list[str]) -> int:
    model = "sonnet"
    effort: str | None = None
    system_prompt: str | None = None
    json_schema: dict[str, Any] | None = None

    i = 0
    while i < len(args):
        arg = args[i]
        if arg == "--model" and i + 1 < len(args):
            model = args[i + 1]
            i += 2
        elif arg == "--effort" and i + 1 < len(args):
            effort = args[i + 1]
            i += 2
        elif arg == "--system-prompt" and i + 1 < len(args):
            system_prompt = args[i + 1]
            i += 2
        elif arg == "--json-schema" and i + 1 < len(args):
            json_schema = json.loads(args[i + 1])
            i += 2
        else:
            i += 1

    prompt = sys.stdin.read()
    effort_tokens = {"low": 32, "medium": 96, "high": 256, "xhigh": 512, "max": 768}.get(effort or "medium", 96)
    in_tokens = max(10, len(prompt) // 4)
    out_tokens = effort_tokens + 48

    usage: dict[str, Any] = {
        "input_tokens": in_tokens,
        "cache_read_input_tokens": 120,
        "cache_creation_input_tokens": 40,
        "cache_creation": {
            "ephemeral_5m_input_tokens": 40,
            "ephemeral_1h_input_tokens": 0,
        },
        "output_tokens": out_tokens,
        "output_tokens_details": {"thinking_tokens": effort_tokens},
        "service_tier": "standard",
        "iterations": [
            {
                "input_tokens": in_tokens,
                "output_tokens": out_tokens,
                "cache_read_input_tokens": 120,
                "cache_creation_input_tokens": 40,
            }
        ],
    }

    if "fail-500" in prompt:
        err_envelope = {
            "type": "result",
            "subtype": "error_during_execution",
            "is_error": True,
            "result": "Simulated Claude CLI failure (fail-500)",
            "session_id": "mock-claude-session-err",
            "usage": usage,
            "modelUsage": {model: {"inputTokens": in_tokens, "outputTokens": out_tokens}},
        }
        sys.stdout.write(json.dumps(err_envelope) + "\n")
        return 1

    stop_reason = "end_turn"
    if "tool-call" in prompt:
        stop_reason = "tool_use"

    result_text = (
        ""
        if "empty-answer" in prompt
        else (
            f"[claude-cli model={model} effort={effort or 'default'}"
            f"{' sys=yes' if system_prompt else ''}] Response to: {prompt.strip()[:120]}"
        )
    )

    model_usage: dict[str, Any] = {
        model: {
            "inputTokens": in_tokens,
            "outputTokens": out_tokens,
            "cacheReadInputTokens": 120,
            "cacheCreationInputTokens": 40,
        }
    }
    if "multi-model" in prompt:
        model_usage["claude-haiku-4-5-20251001"] = {
            "inputTokens": 350,
            "outputTokens": 45,
            "cacheReadInputTokens": 0,
            "cacheCreationInputTokens": 0,
        }

    envelope: dict[str, Any] = {
        "type": "result",
        "subtype": "success",
        "is_error": False,
        "stop_reason": stop_reason,
        "result": result_text,
        "session_id": "mock-claude-session-1",
        "usage": usage,
        "modelUsage": model_usage,
    }
    if json_schema is not None:
        structured = _build_judge_payload(json_schema, prompt)
        envelope["structured_output"] = structured
        envelope["result"] = json.dumps(structured)

    sys.stdout.write(json.dumps(envelope) + "\n")
    return 0


def _run_codex(args: list[str]) -> int:
    model = "gpt-5.4"
    effort: str | None = None
    summary_mode: str | None = None
    dev_instructions: str | None = None
    output_schema: dict[str, Any] | None = None

    i = 0
    while i < len(args):
        arg = args[i]
        if arg in ("-m", "--model") and i + 1 < len(args):
            model = args[i + 1]
            i += 2
        elif arg in ("-c", "--config") and i + 1 < len(args):
            cfg = args[i + 1]
            m_eff = re.match(r"^model_reasoning_effort=(.+)$", cfg)
            m_sum = re.match(r"^model_reasoning_summary=(.+)$", cfg)
            m_dev = re.match(r"^developer_instructions=(.+)$", cfg)
            if m_eff:
                effort = json.loads(m_eff.group(1))
            elif m_sum:
                summary_mode = json.loads(m_sum.group(1))
            elif m_dev:
                dev_instructions = json.loads(m_dev.group(1))
            i += 2
        elif arg == "--output-schema" and i + 1 < len(args):
            schema_path = Path(args[i + 1])
            output_schema = json.loads(schema_path.read_text(encoding="utf-8"))
            i += 2
        else:
            i += 1

    prompt = sys.stdin.read()
    effort_tokens = {
        "minimal": 16,
        "low": 40,
        "medium": 112,
        "high": 280,
        "xhigh": 560,
        "max": 800,
    }.get(effort or "medium", 112)
    uncached_in = max(20, len(prompt) // 4)
    cached_in = 128
    total_in = uncached_in + cached_in
    out_tokens = effort_tokens + 64

    usage = {
        "input_tokens": total_in,
        "cached_input_tokens": cached_in,
        "cache_write_input_tokens": 0,
        "output_tokens": out_tokens,
        "reasoning_output_tokens": effort_tokens,
        "total_tokens": total_in + out_tokens,
    }

    events: list[dict[str, Any]] = [
        {"type": "thread.started", "thread_id": "mock-codex-thread-1"},
        {"type": "turn.started"},
    ]

    if "fail-500" in prompt:
        events.append({"type": "turn.failed", "error": {"message": "Simulated Codex CLI failure (fail-500)"}})
        for evt in events:
            sys.stdout.write(json.dumps(evt) + "\n")
        return 1

    if "tool-call" in prompt:
        events.append(
            {
                "type": "item.completed",
                "item": {"id": "item_tool_1", "type": "command_execution", "command": "ls"},
            }
        )

    events.append(
        {
            "type": "item.completed",
            "item": {
                "id": "item_r_1",
                "type": "reasoning",
                "text": f"Reasoning with effort={effort or 'default'} summary={summary_mode or 'default'}.",
            },
        }
    )

    if output_schema is not None:
        msg_text = json.dumps(_build_judge_payload(output_schema, prompt))
    elif "empty-answer" in prompt:
        msg_text = ""
    else:
        msg_text = (
            f"[codex-cli model={model} effort={effort or 'default'} summary={summary_mode or 'default'}"
            f"{' dev=yes' if dev_instructions else ''}] Response to: {prompt.strip()[:120]}"
        )

    events.append(
        {
            "type": "item.completed",
            "item": {"id": "item_msg_1", "type": "agent_message", "text": msg_text},
        }
    )
    events.append({"type": "turn.completed", "model": model, "usage": usage})

    for evt in events:
        sys.stdout.write(json.dumps(evt) + "\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
