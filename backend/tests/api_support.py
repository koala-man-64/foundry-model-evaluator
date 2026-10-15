"""Shared helpers for API integration tests: a test client wired to the mock Foundry server."""

from __future__ import annotations

import asyncio
import time
from pathlib import Path
from typing import Any

import httpx2
from fastapi.testclient import TestClient

from foundry_eval.api.app import create_app
from foundry_eval.config import AppConfig
from foundry_eval.credentials.store import CredentialStore, MemoryKeyring
from foundry_eval.devtools.mock_foundry import create_mock_app
from foundry_eval.domain.presets import preset_capabilities
from foundry_eval.providers.base import ProviderCall, ProviderResult
from foundry_eval.providers.registry import default_adapters
from foundry_eval.types import ApiStyle, FinishReason

ORIGIN = "http://127.0.0.1:8765"
HEADERS = {"Origin": ORIGIN, "X-FME-Client": "1"}
CANARY = "sk-canary-7f3a9c2e1b5d"


class SlowAdapter:
    """Fake provider for concurrency, cancellation and budget tests."""

    def __init__(self, api_style: ApiStyle, delay: float) -> None:
        self.api_style = api_style
        self.delay = delay
        self.active = 0
        self.max_active = 0
        self.calls = 0

    async def execute(self, call: ProviderCall) -> ProviderResult:
        self.calls += 1
        self.active += 1
        self.max_active = max(self.max_active, self.active)
        try:
            await asyncio.sleep(self.delay)
        finally:
            self.active -= 1
        return ProviderResult(
            ok=True,
            request_body=call.body,
            streamed=False,
            latency_ms=int(self.delay * 1000),
            raw_usage={"prompt_tokens": 100, "completion_tokens": 20, "prompt_tokens_details": {"cached_tokens": 0}},
            output_text="ok",
            finish_reason=FinishReason.COMPLETED,
            http_status=200,
        )


def make_client(
    data_dir: Path,
    *,
    adapters: dict[ApiStyle, Any] | None = None,
    mock_app: Any = None,
    credentials: CredentialStore | None = None,
) -> TestClient:
    mock_app = mock_app or create_mock_app()
    transport = httpx2.ASGITransport(app=mock_app)
    config = AppConfig(data_dir=data_dir, port=8765, static_dir=None)
    app = create_app(
        config,
        adapters=adapters or default_adapters(transport),
        retail_transport=transport,
        credentials=credentials or CredentialStore(MemoryKeyring(), is_test_backend=True),
    )
    return TestClient(app, base_url=ORIGIN, headers=HEADERS)


def create_endpoint(client: TestClient, base_url: str = "https://mock.services.ai.azure.com") -> dict:
    response = client.post("/api/endpoints", json={"name": "Mock", "base_url": base_url, "region": "eastus2"})
    assert response.status_code == 201, response.text
    endpoint = response.json()
    response = client.put(f"/api/endpoints/{endpoint['id']}/key", json={"api_key": CANARY})
    assert response.status_code == 200 and response.json() == {"key_configured": True}
    return endpoint


def create_profile(
    client: TestClient, endpoint_id: str, *, preset: str, api_style: ApiStyle, deployment: str, **extra
) -> dict:
    payload = {
        "endpoint_id": endpoint_id,
        "name": extra.pop("name", deployment),
        "api_style": api_style.value,
        "route": "anthropic" if api_style is ApiStyle.ANTHROPIC_MESSAGES else "openai_v1",
        "deployment": deployment,
        "preset": preset,
        "capabilities": preset_capabilities(preset, api_style).model_dump(),
        **extra,
    }
    response = client.post("/api/profiles", json=payload)
    assert response.status_code == 201, response.text
    return response.json()


def set_manual(client: TestClient, profile_id: str, category: str, price: str) -> None:
    response = client.put(
        f"/api/profiles/{profile_id}/rates/{category}",
        json={"mode": "manual", "price": price, "unit": "1M", "source_url": "https://www.anthropic.com/pricing"},
    )
    assert response.status_code == 200, response.text


def wait_for_run(client: TestClient, run_id: str, timeout: float = 20) -> dict:
    deadline = time.time() + timeout
    while time.time() < deadline:
        progress = client.get(f"/api/runs/{run_id}/progress").json()
        if not progress["active"] and progress["status"] not in ("queued", "running"):
            return client.get(f"/api/runs/{run_id}").json()
        time.sleep(0.05)
    raise AssertionError("run did not finish")


def start_run(client: TestClient, request: dict, key: str = "idem-key-0001") -> str:
    response = client.post("/api/runs", json=request, headers={"Idempotency-Key": key})
    assert response.status_code == 201, response.text
    return response.json()["run_id"]
