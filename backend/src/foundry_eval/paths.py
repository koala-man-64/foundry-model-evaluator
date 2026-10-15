"""Resolve and validate where the application keeps its data.

Application data (SQLite database, logs, instance lock) lives under
``%LOCALAPPDATA%\\FoundryModelEvaluator`` on Windows. The data directory must never be
inside the source repository or a OneDrive-synced folder, because the repository is
typically kept in OneDrive and the database contains prompts, responses and pricing history.
"""

from __future__ import annotations

import os
import sys
from collections.abc import Mapping
from pathlib import Path

APP_DIR_NAME = "FoundryModelEvaluator"
ONEDRIVE_ENV_VARS = ("OneDrive", "OneDriveConsumer", "OneDriveCommercial")


class DataDirError(RuntimeError):
    """Raised when the data directory cannot be used safely."""


def default_data_dir(env: Mapping[str, str] | None = None, platform: str = sys.platform) -> Path:
    """Return the configured data directory (``FME_DATA_DIR`` overrides the default)."""
    env = os.environ if env is None else env
    override = env.get("FME_DATA_DIR")
    if override:
        return Path(override).expanduser()
    if platform == "win32":
        local = env.get("LOCALAPPDATA")
        if not local:
            raise DataDirError("LOCALAPPDATA is not set; set FME_DATA_DIR to choose a data directory.")
        return Path(local) / APP_DIR_NAME
    xdg = env.get("XDG_DATA_HOME")
    base = Path(xdg) if xdg else Path.home() / ".local" / "share"
    return base / APP_DIR_NAME


def runtime_dir(data_dir: Path) -> Path:
    """Where ``scripts\\setup.ps1`` installs Python, the virtual environment and the built web UI."""
    return data_dir / "runtime"


def default_static_dir(data_dir: Path, repo_root: Path | None) -> Path | None:
    """The built web interface: a local ``frontend/dist`` build wins, else the one setup.ps1 installed."""
    candidates = [repo_root / "frontend" / "dist"] if repo_root else []
    candidates.append(runtime_dir(data_dir) / "ui")
    for candidate in candidates:
        if (candidate / "index.html").is_file():
            return candidate
    return candidates[0]


def find_repo_root(start: Path | None = None) -> Path | None:
    """Locate the source checkout (a directory containing both ``backend`` and ``frontend``)."""
    here = (start or Path(__file__)).resolve()
    for candidate in [here, *here.parents]:
        if (candidate / "backend").is_dir() and (candidate / "frontend").is_dir():
            return candidate
    return None


def _norm(path: Path) -> str:
    return os.path.normcase(os.path.normpath(str(path)))


def is_within(child: Path, parent: Path) -> bool:
    """Case-insensitive on Windows; true when ``child`` equals or is below ``parent``."""
    child_s, parent_s = _norm(child), _norm(parent)
    try:
        return os.path.commonpath([child_s, parent_s]) == parent_s
    except ValueError:  # different drives on Windows
        return False


def validate_data_dir(
    data_dir: Path,
    repo_root: Path | None,
    env: Mapping[str, str] | None = None,
) -> Path:
    """Reject data directories inside the repository or any OneDrive root."""
    env = os.environ if env is None else env
    resolved = data_dir.expanduser().resolve()
    if repo_root is not None and is_within(resolved, repo_root.resolve()):
        raise DataDirError(
            f"Refusing to store application data inside the source repository ({resolved}). "
            "Use the default %LOCALAPPDATA% location or set FME_DATA_DIR elsewhere."
        )
    for var in ONEDRIVE_ENV_VARS:
        root = env.get(var)
        if root and is_within(resolved, Path(root).expanduser().resolve()):
            raise DataDirError(
                f"Refusing to store application data under OneDrive ({resolved}). "
                "Use the default %LOCALAPPDATA% location or set FME_DATA_DIR outside OneDrive."
            )
    return resolved


def ensure_data_dir(data_dir: Path) -> Path:
    """Create the data folder. On Windows it inherits the user-only ACL of %LOCALAPPDATA%; elsewhere
    it is made private to the user (mode 0700)."""
    data_dir.mkdir(parents=True, exist_ok=True)
    (data_dir / "logs").mkdir(exist_ok=True)
    if os.name == "posix":
        for folder in (data_dir, data_dir / "logs"):
            folder.chmod(0o700)
    return data_dir
