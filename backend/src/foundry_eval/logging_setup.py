"""Logging configuration: rotating file log in the data directory, with secret redaction."""

from __future__ import annotations

import logging
import logging.handlers
from pathlib import Path

from .credentials.redaction import RedactingFormatter

_FORMAT = "%(asctime)s %(levelname)s %(name)s: %(message)s"
# Third-party HTTP/SDK loggers can include request details at DEBUG level; keep them quiet.
_QUIET_LOGGERS = ("httpx2", "httpcore2", "httpx", "httpcore", "anthropic", "hpack")


def configure_logging(log_dir: Path | None, level: int = logging.INFO) -> None:
    root = logging.getLogger()
    root.setLevel(level)
    for handler in list(root.handlers):
        root.removeHandler(handler)
    formatter = RedactingFormatter(_FORMAT)
    console = logging.StreamHandler()
    console.setFormatter(formatter)
    root.addHandler(console)
    if log_dir is not None:
        log_dir.mkdir(parents=True, exist_ok=True)
        file_handler = logging.handlers.RotatingFileHandler(
            log_dir / "foundry-eval.log", maxBytes=2_000_000, backupCount=5, encoding="utf-8"
        )
        file_handler.setFormatter(formatter)
        root.addHandler(file_handler)
    for name in _QUIET_LOGGERS:
        logging.getLogger(name).setLevel(logging.WARNING)
    # uvicorn installs its own handlers; route them through the redacting formatter as well.
    for name in ("uvicorn", "uvicorn.error", "uvicorn.access"):
        logger = logging.getLogger(name)
        logger.handlers = []
        logger.propagate = True
