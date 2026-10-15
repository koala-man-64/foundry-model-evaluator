"""Fixtures for API integration tests."""

from __future__ import annotations

from collections.abc import Iterator

import pytest
from fastapi.testclient import TestClient

from foundry_eval.devtools.mock_foundry import create_mock_app
from tests.api_support import make_client


@pytest.fixture
def mock_app():
    return create_mock_app()


@pytest.fixture
def client(tmp_path, mock_app) -> Iterator[TestClient]:
    with make_client(tmp_path / "data", mock_app=mock_app) as c:
        yield c
