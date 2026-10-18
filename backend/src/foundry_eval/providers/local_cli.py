"""Local CLI provider adapters for Claude Code (`claude -p`) and Codex (`codex exec`).

Executes single-shot evaluations in an isolated temporary directory with all built-in tools,
MCP servers, skills, hooks, and project instructions disabled so prompts are evaluated purely
as model completions without workspace contamination or side effects.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import os
import shlex
import shutil
import sys
import tempfile
import time
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

from ..types import ApiStyle, FinishReason
from .base import ProviderCall, ProviderResult

_CLAUDE_FINISH: dict[str, FinishReason] = {
    "end_turn": FinishReason.COMPLETED,
    "stop_sequence": FinishReason.COMPLETED,
    "max_tokens": FinishReason.MAX_TOKENS,
    "tool_use": FinishReason.TOOL_CALL,
    "refusal": FinishReason.REFUSAL,
    "model_context_window_exceeded": FinishReason.CONTEXT_EXCEEDED,
}

_CODEX_TOOL_ITEM_TYPES = frozenset(
    {
        "command_execution",
        "mcp_tool_call",
        "function_call",
        "local_shell_call",
        "file_change",
        "web_search_call",
        "custom_tool_call",
    }
)


def _parse_cmd_override(env_var: str) -> list[str] | None:
    raw = os.environ.get(env_var, "").strip()
    if not raw:
        return None
    if raw.startswith("["):
        parsed = json.loads(raw)
        if isinstance(parsed, list) and all(isinstance(x, str) for x in parsed) and parsed:
            return list(parsed)
    parts = shlex.split(raw, posix=(os.name != "nt"))
    return parts if parts else None


def resolve_claude_command(override: Sequence[str] | None = None) -> list[str]:
    """Return the argv prefix to invoke the local `claude` CLI, or raise FileNotFoundError."""
    if override:
        return list(override)
    env_cmd = _parse_cmd_override("FOUNDRY_EVAL_CLAUDE_CMD")
    if env_cmd:
        return env_cmd
    found = shutil.which("claude")
    if found:
        return [found]
    home = Path.home()
    for candidate in (
        home / ".local" / "bin" / "claude.exe",
        home / ".local" / "bin" / "claude.EXE",
        home / ".local" / "bin" / "claude",
    ):
        if candidate.is_file():
            return [str(candidate)]
    raise FileNotFoundError(
        "Claude CLI binary ('claude') was not found on PATH or in ~/.local/bin. "
        "Install Claude Code CLI or set FOUNDRY_EVAL_CLAUDE_CMD."
    )


def resolve_codex_command(override: Sequence[str] | None = None) -> list[str]:
    """Return the argv prefix to invoke the local `codex` CLI, or raise FileNotFoundError."""
    if override:
        return list(override)
    env_cmd = _parse_cmd_override("FOUNDRY_EVAL_CODEX_CMD")
    if env_cmd:
        return env_cmd
    found = shutil.which("codex")
    if found:
        return [found]
    local_app_data = os.environ.get("LOCALAPPDATA")
    if local_app_data:
        codex_bin_root = Path(local_app_data) / "OpenAI" / "Codex" / "bin"
        if codex_bin_root.is_dir():
            candidates = sorted(
                codex_bin_root.rglob("codex.exe"),
                key=lambda p: p.stat().st_mtime,
                reverse=True,
            )
            if candidates:
                return [str(candidates[0])]
    home = Path.home()
    for candidate in (
        home / ".local" / "bin" / "codex.exe",
        home / ".local" / "bin" / "codex",
    ):
        if candidate.is_file():
            return [str(candidate)]
    raise FileNotFoundError(
        "Codex CLI binary ('codex') was not found on PATH or in %LOCALAPPDATA%\\OpenAI\\Codex\\bin. "
        "Install Codex CLI or set FOUNDRY_EVAL_CODEX_CMD."
    )


def cli_kind_from_url(url: str) -> str:
    parts = urlsplit(url.strip())
    host = (parts.hostname or "").lower()
    if host in ("claude", "codex"):
        return host
    return "claude"


async def verify_local_cli(url: str) -> tuple[bool, int | None, str]:
    """Run `<cli> --version` in an isolated temp directory to verify binary availability."""
    kind = cli_kind_from_url(url)
    try:
        cmd = resolve_claude_command() if kind == "claude" else resolve_codex_command()
    except FileNotFoundError as exc:
        return False, None, str(exc)

    env = _isolated_env()
    try:
        with tempfile.TemporaryDirectory(prefix="fme-cli-verify-") as tmpdir:
            proc = await asyncio.create_subprocess_exec(
                *cmd,
                "--version",
                stdin=asyncio.subprocess.DEVNULL,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                cwd=tmpdir,
                env=env,
            )
            try:
                async with asyncio.timeout(5.0):
                    stdout_b, stderr_b = await proc.communicate()
            except TimeoutError:
                await _terminate_process(proc)
                return False, None, f"Timed out running {' '.join(cmd)} --version."
    except OSError as exc:
        return False, None, f"Failed to execute {kind} CLI: {exc}"

    out = stdout_b.decode("utf-8", errors="replace").strip()
    err = stderr_b.decode("utf-8", errors="replace").strip()
    version_text = out or err or "unknown version"
    first_line = version_text.splitlines()[0].strip()
    if proc.returncode == 0:
        return True, 200, f"Verified local {kind} CLI ({first_line}). No tokens billed."
    return False, proc.returncode, f"Local {kind} CLI exited with code {proc.returncode}: {first_line}"


def _isolated_env(extra: Mapping[str, str] | None = None) -> dict[str, str]:
    env = os.environ.copy()
    # Prevent nested Claude Code session detection from blocking `claude -p`.
    for key in ("CLAUDECODE", "CLAUDE_CODE_ENTRYPOINT", "CLAUDE_CODE_SESSION"):
        env.pop(key, None)
    env["CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC"] = "1"
    env["NO_COLOR"] = "1"
    if extra:
        env.update(extra)
    return env


async def _terminate_process(proc: asyncio.subprocess.Process) -> None:
    if proc.returncode is not None:
        return
    if sys.platform == "win32" and proc.pid:
        taskkill = shutil.which("taskkill")
        if taskkill:
            with contextlib.suppress(OSError, TimeoutError):
                killer = await asyncio.create_subprocess_exec(
                    taskkill,
                    "/F",
                    "/T",
                    "/PID",
                    str(proc.pid),
                    stdout=asyncio.subprocess.DEVNULL,
                    stderr=asyncio.subprocess.DEVNULL,
                )
                async with asyncio.timeout(3.0):
                    await killer.wait()
    with contextlib.suppress(ProcessLookupError):
        proc.kill()
    with contextlib.suppress(TimeoutError):
        async with asyncio.timeout(2.0):
            await proc.wait()


class ClaudeCliAdapter:
    """Adapter for local `claude -p --output-format json` execution."""

    api_style = ApiStyle.CLAUDE_CLI

    def __init__(self, command: Sequence[str] | None = None) -> None:
        self._command = list(command) if command else None

    async def execute(self, call: ProviderCall) -> ProviderResult:
        started = time.perf_counter()
        body = call.body
        try:
            cmd = resolve_claude_command(self._command)
        except FileNotFoundError as exc:
            return ProviderResult(
                ok=False,
                request_body=body,
                streamed=False,
                latency_ms=0,
                error_type="cli_not_found",
                error_message=str(exc),
            )

        model = str(body.get("model") or "").strip()
        prompt_text = str(body.get("prompt") or "")
        system_prompt = body.get("system_prompt")
        effort = body.get("effort")
        json_schema = body.get("json_schema")

        argv: list[str] = [
            *cmd,
            "-p",
            "--output-format",
            "json",
            "--safe-mode",
            "--strict-mcp-config",
            "--disable-slash-commands",
            "--no-session-persistence",
            "--permission-mode",
            "dontAsk",
            "--permission-prompts",
            "none",
        ]
        if model:
            argv.extend(["--model", model])
        if isinstance(effort, str) and effort:
            argv.extend(["--effort", effort])
        if isinstance(system_prompt, str) and system_prompt:
            argv.extend(["--system-prompt", system_prompt])
        if isinstance(json_schema, Mapping):
            argv.extend(["--json-schema", json.dumps(dict(json_schema), separators=(",", ":"))])

        extra_env: dict[str, str] = {}
        if call.auth_style in ("api-key", "bearer") and call.api_key:
            argv.append("--bare")
            extra_env["ANTHROPIC_API_KEY"] = call.api_key

        # Place variadic `--tools ""` last so it never consumes subsequent positional args.
        argv.extend(["--tools", ""])

        env = _isolated_env(extra_env)
        try:
            with tempfile.TemporaryDirectory(prefix="fme-claude-") as tmpdir:
                proc = await asyncio.create_subprocess_exec(
                    *argv,
                    stdin=asyncio.subprocess.PIPE,
                    stdout=asyncio.subprocess.PIPE,
                    stderr=asyncio.subprocess.PIPE,
                    cwd=tmpdir,
                    env=env,
                )
                try:
                    async with asyncio.timeout(call.timeouts.total):
                        stdout_b, stderr_b = await proc.communicate(prompt_text.encode("utf-8"))
                except TimeoutError:
                    await _terminate_process(proc)
                    return ProviderResult(
                        ok=False,
                        request_body=body,
                        streamed=False,
                        latency_ms=_elapsed_ms(started),
                        error_type="timeout",
                        error_message=f"claude CLI timed out after {call.timeouts.total:.0f}s",
                    )
                except asyncio.CancelledError:
                    await _terminate_process(proc)
                    raise
        except OSError as exc:
            return ProviderResult(
                ok=False,
                request_body=body,
                streamed=False,
                latency_ms=_elapsed_ms(started),
                error_type="cli_exec_error",
                error_message=f"Failed to start claude CLI: {exc}",
            )

        latency_ms = _elapsed_ms(started)
        stdout_text = stdout_b.decode("utf-8", errors="replace").strip()
        stderr_text = stderr_b.decode("utf-8", errors="replace").strip()

        parsed = _parse_claude_json(stdout_text)
        if parsed is None:
            msg = stderr_text or stdout_text or f"claude CLI exited with code {proc.returncode}"
            return ProviderResult(
                ok=False,
                request_body=body,
                streamed=False,
                latency_ms=latency_ms,
                http_status=proc.returncode,
                error_type="cli_error" if proc.returncode != 0 else "malformed_response",
                error_message=msg[:1000],
            )

        is_error = bool(parsed.get("is_error")) or proc.returncode != 0
        raw_usage = _extract_claude_cli_usage(parsed, requested_model=model)
        response_model = _extract_claude_model(parsed) or model or None
        request_id = parsed.get("session_id") if isinstance(parsed.get("session_id"), str) else None
        raw_stop = parsed.get("stop_reason") if isinstance(parsed.get("stop_reason"), str) else None
        subtype = parsed.get("subtype") if isinstance(parsed.get("subtype"), str) else None

        output_text = ""
        if "structured_output" in parsed and parsed["structured_output"] is not None:
            structured = parsed["structured_output"]
            output_text = structured if isinstance(structured, str) else json.dumps(structured)
        elif isinstance(parsed.get("result"), str):
            output_text = parsed["result"]

        if is_error:
            err_msg = output_text or stderr_text or f"claude CLI error ({subtype or proc.returncode})"
            return ProviderResult(
                ok=False,
                request_body=body,
                streamed=False,
                latency_ms=latency_ms,
                response_json=parsed,
                raw_usage=raw_usage,
                output_text=output_text,
                response_model=response_model,
                request_id=request_id,
                http_status=proc.returncode or 1,
                error_type=subtype or "cli_error",
                error_message=err_msg[:1000],
            )

        finish = _CLAUDE_FINISH.get(raw_stop or "", FinishReason.COMPLETED if not raw_stop else FinishReason.OTHER)
        if subtype == "error_max_turns" or raw_stop == "tool_use":
            finish = FinishReason.TOOL_CALL

        return ProviderResult(
            ok=True,
            request_body=body,
            streamed=False,
            latency_ms=latency_ms,
            response_json=parsed,
            raw_usage=raw_usage,
            output_text=output_text,
            finish_reason=finish,
            raw_finish_reason=raw_stop or subtype,
            response_model=response_model,
            request_id=request_id,
            http_status=0,
        )


def _parse_claude_json(stdout_text: str) -> dict[str, Any] | None:
    if not stdout_text:
        return None
    for candidate in (stdout_text, stdout_text.splitlines()[-1]):
        try:
            loaded = json.loads(candidate)
        except json.JSONDecodeError:
            continue
        if isinstance(loaded, dict):
            return loaded
        if isinstance(loaded, list):
            for item in reversed(loaded):
                if isinstance(item, dict) and item.get("type") == "result":
                    return item
    return None


def _extract_claude_model(parsed: Mapping[str, Any]) -> str | None:
    model_usage = parsed.get("modelUsage")
    if isinstance(model_usage, Mapping) and model_usage:
        first_key = next(iter(model_usage.keys()))
        if isinstance(first_key, str):
            return first_key
    model = parsed.get("model")
    return model if isinstance(model, str) else None


def _extract_claude_cli_usage(parsed: Mapping[str, Any], *, requested_model: str) -> dict[str, Any] | None:
    usage = parsed.get("usage")
    if not isinstance(usage, Mapping):
        return None
    result = dict(usage)
    model_usage = parsed.get("modelUsage")
    if isinstance(model_usage, Mapping) and len(model_usage) > 1:
        # Preserve multi-model breakdown so ClaudeCliUsageNormalizer can flag secondary model spend.
        result["_model_usage"] = {
            str(k): dict(v) for k, v in model_usage.items() if isinstance(v, Mapping)
        }
        result["_requested_model"] = requested_model
    return result


class CodexCliAdapter:
    """Adapter for local `codex exec --json` execution."""

    api_style = ApiStyle.CODEX_CLI

    def __init__(self, command: Sequence[str] | None = None) -> None:
        self._command = list(command) if command else None

    async def execute(self, call: ProviderCall) -> ProviderResult:
        started = time.perf_counter()
        body = call.body
        try:
            cmd = resolve_codex_command(self._command)
        except FileNotFoundError as exc:
            return ProviderResult(
                ok=False,
                request_body=body,
                streamed=True,
                latency_ms=0,
                error_type="cli_not_found",
                error_message=str(exc),
            )

        model = str(body.get("model") or "").strip()
        prompt_text = str(body.get("prompt") or "")
        dev_instructions = body.get("developer_instructions")
        effort = body.get("model_reasoning_effort")
        summary_mode = body.get("model_reasoning_summary")
        output_schema = body.get("output_schema")

        extra_env: dict[str, str] = {}
        if call.auth_style in ("api-key", "bearer") and call.api_key:
            extra_env["OPENAI_API_KEY"] = call.api_key
            extra_env["CODEX_API_KEY"] = call.api_key
        env = _isolated_env(extra_env)

        try:
            with (
                tempfile.TemporaryDirectory(prefix="fme-codex-cwd-") as tmp_cwd,
                tempfile.TemporaryDirectory(prefix="fme-codex-meta-") as tmp_meta,
            ):
                argv: list[str] = [
                    *cmd,
                    "exec",
                    "--json",
                    "--ephemeral",
                    "--skip-git-repo-check",
                    "--sandbox",
                    "read-only",
                    "-a",
                    "never",
                    "-c",
                    "features.shell_tool=false",
                    "--color",
                    "never",
                    "-C",
                    tmp_cwd,
                ]
                if model:
                    argv.extend(["-m", model])
                if isinstance(effort, str) and effort:
                    argv.extend(["-c", f"model_reasoning_effort={json.dumps(effort)}"])
                if isinstance(summary_mode, str) and summary_mode:
                    argv.extend(["-c", f"model_reasoning_summary={json.dumps(summary_mode)}"])
                if isinstance(dev_instructions, str) and dev_instructions:
                    argv.extend(["-c", f"developer_instructions={json.dumps(dev_instructions)}"])
                if isinstance(output_schema, Mapping):
                    schema_path = Path(tmp_meta) / "output_schema.json"
                    schema_path.write_text(json.dumps(dict(output_schema)), encoding="utf-8")
                    argv.extend(["--output-schema", str(schema_path)])
                argv.append("-")

                proc = await asyncio.create_subprocess_exec(
                    *argv,
                    stdin=asyncio.subprocess.PIPE,
                    stdout=asyncio.subprocess.PIPE,
                    stderr=asyncio.subprocess.PIPE,
                    cwd=tmp_cwd,
                    env=env,
                )

                events: list[dict[str, Any]] = []
                ttft_ms: int | None = None
                stderr_b = b""
                try:
                    async with asyncio.timeout(call.timeouts.total):
                        assert proc.stdin is not None
                        assert proc.stdout is not None
                        assert proc.stderr is not None
                        proc.stdin.write(prompt_text.encode("utf-8"))
                        await proc.stdin.drain()
                        proc.stdin.close()

                        while True:
                            line_b = await proc.stdout.readline()
                            if not line_b:
                                break
                            line = line_b.decode("utf-8", errors="replace").strip()
                            if not line:
                                continue
                            try:
                                evt = json.loads(line)
                            except json.JSONDecodeError:
                                continue
                            if isinstance(evt, dict):
                                events.append(evt)
                                if ttft_ms is None and evt.get("type") in (
                                    "item.started",
                                    "item.updated",
                                    "item.completed",
                                ):
                                    ttft_ms = _elapsed_ms(started)
                        stderr_b = await proc.stderr.read()
                        await proc.wait()
                except TimeoutError:
                    await _terminate_process(proc)
                    return ProviderResult(
                        ok=False,
                        request_body=body,
                        streamed=True,
                        latency_ms=_elapsed_ms(started),
                        ttft_ms=ttft_ms,
                        error_type="timeout",
                        error_message=f"codex CLI timed out after {call.timeouts.total:.0f}s",
                    )
                except asyncio.CancelledError:
                    await _terminate_process(proc)
                    raise
        except OSError as exc:
            return ProviderResult(
                ok=False,
                request_body=body,
                streamed=True,
                latency_ms=_elapsed_ms(started),
                error_type="cli_exec_error",
                error_message=f"Failed to start codex CLI: {exc}",
            )

        latency_ms = _elapsed_ms(started)
        stderr_text = stderr_b.decode("utf-8", errors="replace").strip()
        return _build_codex_result(
            body=body,
            events=events,
            returncode=proc.returncode or 0,
            stderr_text=stderr_text,
            latency_ms=latency_ms,
            ttft_ms=ttft_ms,
            requested_model=model,
        )


def _build_codex_result(
    *,
    body: dict[str, Any],
    events: list[dict[str, Any]],
    returncode: int,
    stderr_text: str,
    latency_ms: int,
    ttft_ms: int | None,
    requested_model: str,
) -> ProviderResult:
    if not events and returncode != 0:
        return ProviderResult(
            ok=False,
            request_body=body,
            streamed=True,
            latency_ms=latency_ms,
            ttft_ms=ttft_ms,
            http_status=returncode,
            error_type="cli_error",
            error_message=(stderr_text or f"codex CLI exited with code {returncode}")[:1000],
        )

    text_parts: list[str] = []
    reasoning_parts: list[str] = []
    raw_usage: dict[str, Any] | None = None
    thread_id: str | None = None
    response_model: str | None = requested_model or None
    tool_called = False
    error_messages: list[str] = []
    raw_finish: str | None = None

    for evt in events:
        etype = evt.get("type")
        if etype == "thread.started" and isinstance(evt.get("thread_id"), str):
            thread_id = evt["thread_id"]
        elif etype == "item.completed":
            item = evt.get("item")
            if isinstance(item, Mapping):
                itype = item.get("type")
                if itype in ("agent_message", "message"):
                    txt = item.get("text")
                    if isinstance(txt, str) and txt:
                        text_parts.append(txt)
                    elif isinstance(item.get("content"), list):
                        for block in item["content"]:
                            if isinstance(block, Mapping) and isinstance(block.get("text"), str):
                                text_parts.append(block["text"])
                elif itype == "reasoning":
                    txt = item.get("text")
                    if isinstance(txt, str) and txt.strip():
                        reasoning_parts.append(txt.strip())
                    elif isinstance(item.get("summary"), list):
                        for s in item["summary"]:
                            if isinstance(s, Mapping) and isinstance(s.get("text"), str):
                                reasoning_parts.append(s["text"].strip())
                            elif isinstance(s, str) and s.strip():
                                reasoning_parts.append(s.strip())
                elif itype in _CODEX_TOOL_ITEM_TYPES:
                    tool_called = True
        elif etype == "turn.completed":
            raw_finish = "turn.completed"
            usage = evt.get("usage")
            if isinstance(usage, Mapping):
                raw_usage = dict(usage)
            if isinstance(evt.get("model"), str):
                response_model = evt["model"]
        elif etype in ("token_usage_record", "token_count"):
            if isinstance(evt.get("model"), str):
                response_model = evt["model"]
            usage = evt.get("usage")
            if isinstance(usage, Mapping):
                raw_usage = dict(usage)
        elif etype in ("turn.failed", "error"):
            raw_finish = str(etype)
            err = evt.get("error")
            if isinstance(err, Mapping) and isinstance(err.get("message"), str):
                error_messages.append(err["message"])
            elif isinstance(evt.get("message"), str):
                error_messages.append(evt["message"])

    output_text = "\n\n".join(text_parts)
    reasoning_summary = "\n\n".join(p for p in reasoning_parts if p) or None
    response_json: dict[str, Any] = {
        "thread_id": thread_id,
        "model": response_model,
        "events": events,
        "usage": raw_usage,
    }

    if error_messages or returncode != 0:
        msg = "; ".join(error_messages) or stderr_text or f"codex CLI exited with code {returncode}"
        return ProviderResult(
            ok=False,
            request_body=body,
            streamed=True,
            latency_ms=latency_ms,
            ttft_ms=ttft_ms,
            response_json=response_json,
            raw_usage=raw_usage,
            output_text=output_text,
            reasoning_summary=reasoning_summary,
            finish_reason=FinishReason.ERROR,
            raw_finish_reason=raw_finish,
            response_model=response_model,
            request_id=thread_id,
            http_status=returncode or 1,
            error_type="cli_error",
            error_message=msg[:1000],
        )

    finish = FinishReason.TOOL_CALL if tool_called else FinishReason.COMPLETED
    return ProviderResult(
        ok=True,
        request_body=body,
        streamed=True,
        latency_ms=latency_ms,
        ttft_ms=ttft_ms,
        response_json=response_json,
        raw_usage=raw_usage,
        output_text=output_text,
        reasoning_summary=reasoning_summary,
        finish_reason=finish,
        raw_finish_reason=raw_finish or "completed",
        response_model=response_model,
        request_id=thread_id,
        http_status=0,
    )


def _elapsed_ms(started: float) -> int:
    return max(0, int((time.perf_counter() - started) * 1000))
