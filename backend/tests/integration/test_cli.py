from __future__ import annotations

import httpx2

from foundry_eval import cli
from foundry_eval import services as services_module
from foundry_eval.credentials.store import CredentialStore, MemoryKeyring
from foundry_eval.devtools.mock_foundry import create_mock_app
from foundry_eval.providers.registry import default_adapters
from foundry_eval.types import ApiStyle
from tests.api_support import create_endpoint, create_profile, make_client, set_manual

CLAUDE_RATES = {
    "input_uncached": "3",
    "cache_read": "0.3",
    "cache_write_5m": "3.75",
    "cache_write_1h": "6",
    "output": "15",
}


def test_smoke_makes_budgeted_calls_judges_them_and_keeps_settings(tmp_path, monkeypatch, capsys):
    data = tmp_path / "data"
    mock = create_mock_app()
    store = CredentialStore(MemoryKeyring(), is_test_backend=True)
    with make_client(data, mock_app=mock, credentials=store) as client:
        endpoint = create_endpoint(client)
        for name in ("Claude smoke", "Claude judge"):
            profile = create_profile(
                client,
                endpoint["id"],
                preset="claude-adaptive",
                api_style=ApiStyle.ANTHROPIC_MESSAGES,
                deployment=name.lower().replace(" ", "-"),
                name=name,
            )
            for category, price in CLAUDE_RATES.items():
                set_manual(client, profile["id"], category, price)
        concurrency = client.get("/api/settings").json()["settings"]["concurrency"]

    transport = httpx2.ASGITransport(app=mock)
    monkeypatch.setenv("FME_DATA_DIR", str(data))
    monkeypatch.setattr(CredentialStore, "from_environment", classmethod(lambda cls, mode, platform="": store))
    monkeypatch.setattr(services_module, "default_adapters", lambda: default_adapters(transport))
    code = cli.main(["smoke", "--profile", "Claude smoke", "--budget", "0.05", "--judge", "Claude judge"])
    out = capsys.readouterr().out
    assert code == 0, out
    assert "Claude smoke: succeeded" in out
    assert "Judge (Claude judge): succeeded" in out
    assert "A = Claude smoke: weighted" in out
    assert "Spent (upper bound):" in out and "of budget 0.05" in out
    assert mock.state.mock.requests["/anthropic/v1/messages"] == 2

    with make_client(data, mock_app=mock, credentials=store) as client:
        assert client.get("/api/settings").json()["settings"]["concurrency"] == concurrency
        runs = client.get("/api/runs", params={"kind": "smoke"}).json()
        assert len(runs) == 1 and runs[0]["judging_cost"]["counted"] == 1


def test_smoke_requires_a_positive_budget(capsys):
    assert cli.main(["smoke", "--profile", "x", "--budget", "0"]) == 2
    assert "--budget must be greater than zero" in capsys.readouterr().err
