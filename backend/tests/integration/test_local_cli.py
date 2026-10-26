"""Integration tests for Local CLI providers (`claude_cli` and `codex_cli`)."""

from __future__ import annotations

import json
import sys
import time
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from foundry_eval.domain.generation import GenerationSettings
from foundry_eval.domain.presets import preset_capabilities
from foundry_eval.domain.profiles import Endpoint, ModelProfile, ProfileFields, validate_base_url
from foundry_eval.providers.base import Prompt
from foundry_eval.providers.local_cli import _extract_claude_model
from foundry_eval.providers.request_builder import SettingsValidationError, build_body
from foundry_eval.types import ApiStyle
from tests.api_support import make_client, set_manual, start_run, wait_for_run


@pytest.fixture
def cli_env(monkeypatch: pytest.MonkeyPatch) -> None:
    claude_cmd = json.dumps([sys.executable, "-m", "foundry_eval.devtools.mock_cli", "claude"])
    codex_cmd = json.dumps([sys.executable, "-m", "foundry_eval.devtools.mock_cli", "codex"])
    monkeypatch.setenv("FOUNDRY_EVAL_CLAUDE_CMD", claude_cmd)
    monkeypatch.setenv("FOUNDRY_EVAL_CODEX_CMD", codex_cmd)


@pytest.fixture
def cli_client(tmp_path: Path, cli_env: None):
    with make_client(tmp_path / "data") as test_client:
        yield test_client


def test_validate_base_url_accepts_local_cli_schemes() -> None:
    assert validate_base_url("local://claude") == "local://claude"
    assert validate_base_url("local://CODEX/") == "local://codex"
    with pytest.raises(ValueError, match="local://claude or local://codex"):
        validate_base_url("local://other")


def test_build_body_for_claude_and_codex_cli() -> None:
    claude_caps = preset_capabilities("claude-cli", ApiStyle.CLAUDE_CLI, 16384)
    claude_fields = ProfileFields(
        endpoint_id="ep-1",
        name="Claude CLI",
        api_style=ApiStyle.CLAUDE_CLI,
        route="cli",
        deployment="sonnet",
        preset="claude-cli",
        capabilities=claude_caps,
        streaming=False,
    )
    claude_prof = ModelProfile(
        id="p-claude",
        created_at="2026-01-01T00:00:00Z",
        updated_at="2026-01-01T00:00:00Z",
        **claude_fields.model_dump(),
    )
    body = build_body(
        claude_prof,
        GenerationSettings(effort="high"),
        Prompt(user="Explain invariants", system="Be concise"),
        json_schema={"type": "object"},
    )
    assert body == {
        "cli": "claude",
        "model": "sonnet",
        "prompt": "Explain invariants",
        "system_prompt": "Be concise",
        "effort": "high",
        "json_schema": {"type": "object"},
        "stream": False,
    }
    with pytest.raises(SettingsValidationError):
        build_body(claude_prof, GenerationSettings(temperature=0.5), Prompt(user="hi"))

    codex_caps = preset_capabilities("codex-cli", ApiStyle.CODEX_CLI, 16384)
    codex_fields = ProfileFields(
        endpoint_id="ep-2",
        name="Codex CLI",
        api_style=ApiStyle.CODEX_CLI,
        route="cli",
        deployment="gpt-5.4",
        preset="codex-cli",
        capabilities=codex_caps,
        streaming=True,
    )
    codex_prof = ModelProfile(
        id="p-codex",
        created_at="2026-01-01T00:00:00Z",
        updated_at="2026-01-01T00:00:00Z",
        **codex_fields.model_dump(),
    )
    c_body = build_body(
        codex_prof,
        GenerationSettings(effort="xhigh", reasoning_summary="detailed"),
        Prompt(user="Write a parser", system="Follow PEP 8"),
    )
    assert c_body == {
        "cli": "codex",
        "model": "gpt-5.4",
        "prompt": "Write a parser",
        "developer_instructions": "Follow PEP 8",
        "model_reasoning_effort": "xhigh",
        "model_reasoning_summary": "detailed",
        "stream": True,
    }


def _profile(api_style: ApiStyle, preset: str, deployment: str, model_version: str = "") -> ModelProfile:
    return ModelProfile(
        id=f"p-{deployment}-{model_version}",
        endpoint_id="ep-1",
        name=deployment,
        api_style=api_style,
        route="cli" if api_style.is_cli else "openai_v1",
        deployment=deployment,
        model_version=model_version,
        preset=preset,
        capabilities=preset_capabilities(preset, api_style),
        created_at="2026-01-01T00:00:00Z",
        updated_at="2026-01-01T00:00:00Z",
    )


def test_cli_version_pins_a_model_snapshot() -> None:
    prompt = Prompt(user="hi")
    claude = _profile(ApiStyle.CLAUDE_CLI, "claude-cli", "claude-opus-4-1", " 20250805 ")
    codex = _profile(ApiStyle.CODEX_CLI, "codex-cli", "gpt-4.1", "2025-04-14")
    assert build_body(claude, GenerationSettings(), prompt)["model"] == "claude-opus-4-1-20250805"
    assert build_body(codex, GenerationSettings(), prompt)["model"] == "gpt-4.1-2025-04-14"
    # Without a version the model goes as entered, so an alias such as opus still resolves in the CLI.
    assert build_body(_profile(ApiStyle.CLAUDE_CLI, "claude-cli", "opus"), GenerationSettings(), prompt)["model"] == (
        "opus"
    )


def test_foundry_version_is_recorded_not_sent() -> None:
    endpoint = Endpoint(
        id="ep-1",
        name="Foundry",
        base_url="https://mock.services.ai.azure.com",
        region="eastus2",
        auth_style="api-key",
        credential_ref="ref-1",
        key_set_at=None,
        created_at="2026-01-01T00:00:00Z",
        updated_at="2026-01-01T00:00:00Z",
    )
    old = _profile(ApiStyle.OPENAI_CHAT, "generic-chat", "my-gpt41", "2025-04-14")
    new = _profile(ApiStyle.OPENAI_CHAT, "generic-chat", "my-gpt41", "2025-09-30")
    # The deployment fixes the version, so the request names the deployment only.
    assert build_body(old, GenerationSettings(), Prompt(user="hi"))["model"] == "my-gpt41"
    # The recorded version still keeps the two apart in comparisons.
    assert old.pricing_identity(endpoint, "USD") != new.pricing_identity(endpoint, "USD")


def test_claude_cli_reports_the_model_that_answered_not_background_usage() -> None:
    usage = {
        "claude-haiku-4-5-20251001": {"inputTokens": 350, "outputTokens": 45},
        "claude-opus-4-5-20251101": {"inputTokens": 50, "outputTokens": 300},
    }
    assert _extract_claude_model({"modelUsage": usage}) == "claude-opus-4-5-20251101"
    assert _extract_claude_model({"modelUsage": {"opus": {}}}) == "opus"
    assert _extract_claude_model({"model": "sonnet"}) == "sonnet"


def test_a_cli_variant_runs_its_pinned_snapshot_as_its_own_candidate(cli_client: TestClient) -> None:
    cli_client.post("/api/endpoints/seed-local-cli")
    profiles = {p["name"]: p for p in cli_client.get("/api/profiles").json()}
    source = profiles["Claude Opus (CLI)"]
    fields = {k: v for k, v in source.items() if k not in ("id", "archived", "created_at", "updated_at")}
    created = cli_client.post(
        "/api/profiles",
        json={**fields, "name": "Opus 4.1 snapshot", "deployment": "claude-opus-4-1", "model_version": "20250805"},
    )
    assert created.status_code == 201, created.text
    variant = created.json()
    for profile in (source, variant):
        _set_manual_rates(cli_client, profile["id"], "claude_cli")

    run_payload = {
        "kind": "compare",
        "prompts": [{"user": "What is 6 * 7?"}],
        "candidates": [{"profile_id": source["id"]}, {"profile_id": variant["id"]}],
    }
    detail = wait_for_run(cli_client, start_run(cli_client, run_payload, key="cli-variant-1"))
    assert detail["run"]["status"] == "completed"
    candidates = {c["profile_id"]: c for c in detail["candidates"]}
    assert candidates[variant["id"]]["model_version"] == "20250805"
    assert candidates[source["id"]]["candidate_key"] != candidates[variant["id"]]["candidate_key"]

    attempts = {a["candidate_id"]: a for a in detail["attempts"]}
    pinned = attempts[candidates[variant["id"]]["id"]]
    assert "[claude-cli model=claude-opus-4-1-20250805 " in pinned["output_text"]
    assert pinned["response_model"] == "claude-opus-4-1-20250805"
    assert pinned["cost"]["status"] == "complete"
    raw = cli_client.get(f"/api/attempts/{pinned['id']}/raw").json()
    assert raw["request_body"]["model"] == "claude-opus-4-1-20250805"
    assert "[claude-cli model=opus " in attempts[candidates[source["id"]]["id"]]["output_text"]


def _set_manual_rates(client: TestClient, profile_id: str, api_style: str) -> None:
    categories = (
        ["input_uncached", "cache_read", "cache_write_5m", "cache_write_1h", "output"]
        if api_style == "claude_cli"
        else ["input_uncached", "cache_read", "output"]
    )
    for cat in categories:
        set_manual(client, profile_id, cat, "2.00")


def _wait_for_evaluation(client: TestClient, eval_id: str, timeout: float = 20) -> dict:
    deadline = time.time() + timeout
    while time.time() < deadline:
        ev = client.get(f"/api/evaluations/{eval_id}").json()
        if ev["status"] not in ("queued", "running"):
            return ev
        time.sleep(0.05)
    raise AssertionError("evaluation did not finish")


def test_seed_verify_run_and_judge_with_local_clis(cli_client: TestClient) -> None:
    # 1. Seed local CLIs idempotently
    seed1 = cli_client.post("/api/endpoints/seed-local-cli")
    assert seed1.status_code == 200, seed1.text
    s1 = seed1.json()
    assert s1["endpoints_created"] == 2
    assert s1["profiles_created"] == 6

    seed2 = cli_client.post("/api/endpoints/seed-local-cli")
    assert seed2.status_code == 200
    assert seed2.json()["endpoints_created"] == 0
    assert seed2.json()["profiles_created"] == 0

    # 2. Verify both CLI endpoints via mock_cli --version
    for ep in s1["endpoints"]:
        assert ep["auth_style"] == "local_cli"
        assert ep["key_configured"] is True
        v = cli_client.post(f"/api/endpoints/{ep['id']}/verify")
        assert v.status_code == 200
        assert v.json()["ok"] is True
        assert "mock" in v.json()["message"]

    # 3. Configure manual rates on Claude Sonnet (CLI) and Codex GPT-5.4 (CLI)
    profiles = {p["name"]: p for p in cli_client.get("/api/profiles").json()}
    claude_prof = profiles["Claude Sonnet (CLI)"]
    codex_prof = profiles["Codex GPT-5.4 (CLI)"]
    _set_manual_rates(cli_client, claude_prof["id"], "claude_cli")
    _set_manual_rates(cli_client, codex_prof["id"], "codex_cli")

    # 4. Preview and run a comparison across Claude CLI (low vs high effort) and Codex CLI (high effort)
    run_payload = {
        "kind": "compare",
        "title": "Local CLI Effort Comparison",
        "prompts": [{"title": "Math", "system": "Be brief", "user": "What is 6 * 7?"}],
        "candidates": [
            {"profile_id": claude_prof["id"], "settings": {"effort": "low"}},
            {"profile_id": claude_prof["id"], "settings": {"effort": "high"}},
            {"profile_id": codex_prof["id"], "settings": {"effort": "high", "reasoning_summary": "concise"}},
        ],
        "trials": 1,
        "budget": "5.00",
    }
    preview = cli_client.post("/api/runs/preview", json=run_payload)
    assert preview.status_code == 200, preview.text
    assert preview.json()["ok"] is True

    run_id = start_run(cli_client, run_payload, key="cli-run-1")
    detail = wait_for_run(cli_client, run_id)

    assert detail["run"]["status"] == "completed"
    attempts = detail["attempts"]
    assert len(attempts) == 3
    by_label = {c["id"]: c["label"] for c in detail["candidates"]}
    outputs = {by_label[a["candidate_id"]]: a for a in attempts}

    low_att = outputs["Claude Sonnet (CLI) (effort=low)"]
    high_att = outputs["Claude Sonnet (CLI) (effort=high)"]
    codex_att = outputs["Codex GPT-5.4 (CLI)"]

    assert "[claude-cli model=sonnet effort=low sys=yes]" in low_att["output_text"]
    assert "[claude-cli model=sonnet effort=high sys=yes]" in high_att["output_text"]
    assert high_att["usage"]["reasoning"] > low_att["usage"]["reasoning"]
    assert low_att["cost"]["status"] == "complete"
    assert high_att["cost"]["status"] == "complete"

    assert "[codex-cli model=gpt-5.4 effort=high summary=concise dev=yes]" in codex_att["output_text"]
    assert "effort=high summary=concise" in (codex_att["reasoning_summary"] or "")
    assert codex_att["usage"]["reasoning"] == 280
    assert codex_att["cost"]["status"] == "complete"

    # 5. Judge the run using Claude Sonnet (CLI) (--json-schema) and Codex GPT-5.4 (CLI) (--output-schema)
    for idx, judge_prof in enumerate((claude_prof, codex_prof)):
        j_req = {"judge_profile_id": judge_prof["id"], "settings": {"effort": "low"}}
        j_prev = cli_client.post(f"/api/runs/{run_id}/evaluations/preview", json=j_req)
        assert j_prev.status_code == 200, j_prev.text
        assert j_prev.json()["ok"] is True

        j_create = cli_client.post(
            f"/api/runs/{run_id}/evaluations",
            json=j_req,
            headers={"Idempotency-Key": f"cli-judge-{idx}"},
        )
        assert j_create.status_code in (200, 201), j_create.text
        eval_ids = j_create.json()["evaluation_ids"]
        assert len(eval_ids) == 1
        ev = _wait_for_evaluation(cli_client, eval_ids[0])
        assert ev["status"] == "succeeded", ev
        assert len(ev["candidates"]) == 3


def test_claude_cli_multi_model_and_tool_call_handling(cli_client: TestClient) -> None:
    seed = cli_client.post("/api/endpoints/seed-local-cli").json()
    assert seed["endpoints"]
    profiles = {p["name"]: p for p in cli_client.get("/api/profiles").json()}
    claude_prof = profiles["Claude Sonnet (CLI)"]
    _set_manual_rates(cli_client, claude_prof["id"], "claude_cli")

    run_payload = {
        "kind": "compare",
        "title": "CLI Edge Cases",
        "prompts": [
            {"title": "MultiModel", "user": "Trigger multi-model usage"},
            {"title": "ToolCall", "user": "Trigger tool-call refusal"},
        ],
        "candidates": [{"profile_id": claude_prof["id"], "settings": {"effort": "medium"}}],
        "trials": 1,
    }
    run_id = start_run(cli_client, run_payload, key="cli-edge-1")
    detail = wait_for_run(cli_client, run_id)

    assert detail["run"]["status"] == "completed_with_errors"
    prompts_by_id = {p["id"]: p["title"] for p in detail["prompts"]}
    by_prompt = {prompts_by_id[a["prompt_id"]]: a for a in detail["attempts"]}

    mm = by_prompt["MultiModel"]
    assert mm["status"] == "succeeded"
    assert mm["cost"]["status"] == "incomplete"
    assert any("claude-haiku-4-5" in r for r in mm["cost"]["reasons"])

    tc = by_prompt["ToolCall"]
    assert tc["status"] == "failed"
    assert tc["finish_reason"] == "tool_call"
    assert tc["error_type"] == "no_answer"
