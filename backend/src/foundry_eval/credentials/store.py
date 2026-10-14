"""API key storage backed by the OS credential store (Windows Credential Manager).

Keys are written and read only through this module. They are never returned by the HTTP API,
never written to SQLite or logs, and never sent to the browser.
"""

from __future__ import annotations

import contextlib
import sys
from typing import Any

import keyring
import keyring.errors
from keyring.backend import KeyringBackend

from .redaction import REGISTRY

SERVICE_NAME = "FoundryModelEvaluator"
_MAX_KEY_LENGTH = 2048
_REJECTED_BACKEND_MODULES = ("keyring.backends.fail", "keyring.backends.null", "keyrings.alt")


class CredentialStoreError(RuntimeError):
    """The credential store is unavailable or unsafe."""


class MemoryKeyring(KeyringBackend):
    """Process-local keyring used only by automated tests (``FME_KEYRING=memory``)."""

    priority = 0  # never auto-selected

    def __init__(self) -> None:
        super().__init__()  # type: ignore[no-untyped-call]
        self._values: dict[tuple[str, str], str] = {}

    def get_password(self, service: str, username: str) -> str | None:
        return self._values.get((service, username))

    def set_password(self, service: str, username: str, password: str) -> None:
        self._values[(service, username)] = password

    def delete_password(self, service: str, username: str) -> None:
        if (service, username) not in self._values:
            raise keyring.errors.PasswordDeleteError("not found")
        del self._values[(service, username)]


def _username(credential_ref: str) -> str:
    return f"credential:{credential_ref}"


class CredentialStore:
    def __init__(self, backend: Any, *, is_test_backend: bool = False) -> None:
        self._backend = backend
        self.is_test_backend = is_test_backend

    @classmethod
    def from_environment(cls, mode: str | None, platform: str = sys.platform) -> CredentialStore:
        if mode == "memory":
            return cls(MemoryKeyring(), is_test_backend=True)
        if mode not in (None, "", "system"):
            raise CredentialStoreError(f"Unknown FME_KEYRING mode {mode!r}.")
        if platform == "win32":
            from keyring.backends.Windows import WinVaultKeyring

            return cls(WinVaultKeyring())  # type: ignore[no-untyped-call]
        backend = keyring.get_keyring()
        module = type(backend).__module__
        if any(module.startswith(rejected) for rejected in _REJECTED_BACKEND_MODULES):
            raise CredentialStoreError(
                f"No secure credential store is available (keyring backend {module}). "
                "On Windows the Windows Credential Manager is used automatically."
            )
        return cls(backend)

    @property
    def backend_name(self) -> str:
        backend = type(self._backend)
        return f"{backend.__module__}.{backend.__qualname__}"

    def set(self, credential_ref: str, api_key: str) -> None:
        key = api_key.strip()
        if not key:
            raise ValueError("API key must not be empty.")
        if len(key) > _MAX_KEY_LENGTH:
            raise ValueError("API key is too long.")
        if any(ch.isspace() for ch in key):
            raise ValueError("API key must not contain whitespace.")
        REGISTRY.register(key)
        self._backend.set_password(SERVICE_NAME, _username(credential_ref), key)

    def get(self, credential_ref: str) -> str | None:
        value: str | None = self._backend.get_password(SERVICE_NAME, _username(credential_ref))
        if value:
            REGISTRY.register(value)
        return value

    def has(self, credential_ref: str) -> bool:
        return self.get(credential_ref) is not None

    def delete(self, credential_ref: str) -> None:
        with contextlib.suppress(keyring.errors.PasswordDeleteError):
            self._backend.delete_password(SERVICE_NAME, _username(credential_ref))
