"""Health and application settings."""

from __future__ import annotations

from fastapi import APIRouter
from pydantic import BaseModel

from ... import __version__
from ...repos.settings import AppSettings
from ..deps import ServicesDep

router = APIRouter(prefix="/api", tags=["system"])


class Health(BaseModel):
    status: str
    version: str


class SettingsOut(BaseModel):
    settings: AppSettings
    version: str
    data_dir: str
    keyring_backend: str
    keyring_error: str | None
    keyring_is_test_backend: bool


@router.get("/health", response_model=Health)
def health() -> Health:
    return Health(status="ok", version=__version__)


def _settings_out(services: ServicesDep) -> SettingsOut:
    return SettingsOut(
        settings=services.settings.get(),
        version=__version__,
        data_dir=str(services.config.data_dir),
        keyring_backend=services.credentials.backend_name,
        keyring_error=services.credential_error,
        keyring_is_test_backend=services.credentials.is_test_backend,
    )


@router.get("/settings", response_model=SettingsOut)
def get_settings(services: ServicesDep) -> SettingsOut:
    return _settings_out(services)


@router.put("/settings", response_model=SettingsOut)
def put_settings(payload: AppSettings, services: ServicesDep) -> SettingsOut:
    services.settings.save(payload)
    return _settings_out(services)
