"""Secret redaction for logs and persisted error text (defence in depth).

API keys are never intentionally logged or stored. As a second line of defence every key the
app reads or writes is registered here, and log output plus any error text persisted to SQLite
is scrubbed of registered secrets and common auth-header shapes.
"""

from __future__ import annotations

import logging
import re
import threading

REDACTED = "[REDACTED]"
_MIN_SECRET_LENGTH = 6
_HEADER_PATTERN = re.compile(
    r"(?i)\b(api-key|x-api-key|ocp-apim-subscription-key|authorization)(\"?\s*[:=]\s*\"?)"
    r"(bearer\s+)?([^\s\"',;}]+)"
)
_BEARER_PATTERN = re.compile(r"(?i)\bbearer\s+[A-Za-z0-9._~+/=-]{8,}")


class SecretRegistry:
    def __init__(self) -> None:
        self._secrets: set[str] = set()
        self._lock = threading.Lock()

    def register(self, secret: str | None) -> None:
        if secret and len(secret) >= _MIN_SECRET_LENGTH:
            with self._lock:
                self._secrets.add(secret)

    def forget(self, secret: str | None) -> None:
        if secret:
            with self._lock:
                self._secrets.discard(secret)

    def redact(self, text: str) -> str:
        if not text:
            return text
        with self._lock:
            secrets = sorted(self._secrets, key=len, reverse=True)
        for secret in secrets:
            if secret in text:
                text = text.replace(secret, REDACTED)
        text = _HEADER_PATTERN.sub(lambda m: f"{m.group(1)}{m.group(2)}{m.group(3) or ''}{REDACTED}", text)
        return _BEARER_PATTERN.sub(f"Bearer {REDACTED}", text)


REGISTRY = SecretRegistry()


def sanitize_error_text(text: str | None, limit: int = 4000) -> str | None:
    """Redact secrets from error text before it is stored or returned (error text only)."""
    if text is None:
        return None
    cleaned = REGISTRY.redact(str(text))
    if len(cleaned) > limit:
        cleaned = cleaned[:limit] + " …[error text shortened]"
    return cleaned


class RedactingFormatter(logging.Formatter):
    """Formatter that scrubs the fully formatted record, including tracebacks."""

    def format(self, record: logging.LogRecord) -> str:
        return REGISTRY.redact(super().format(record))
