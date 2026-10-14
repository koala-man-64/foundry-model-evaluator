"""Runtime configuration (environment and CLI); never contains secrets."""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path

from .paths import default_data_dir, default_static_dir, find_repo_root, validate_data_dir
from .pricing.retail import DEFAULT_RETAIL_PRICES_URL

ALLOWED_BIND_HOSTS = ("127.0.0.1",)
DEFAULT_PORT = 8765
VITE_DEV_PORT = 5173


class ConfigError(ValueError):
    pass


@dataclass(frozen=True)
class AppConfig:
    data_dir: Path
    host: str = "127.0.0.1"
    port: int = DEFAULT_PORT
    dev: bool = False
    keyring_mode: str | None = None
    retail_prices_url: str = DEFAULT_RETAIL_PRICES_URL
    static_dir: Path | None = None
    repo_root: Path | None = field(default=None)

    def __post_init__(self) -> None:
        if self.host not in ALLOWED_BIND_HOSTS:
            raise ConfigError(f"The app only listens on loopback (127.0.0.1); refusing host {self.host!r}.")
        if not 1024 <= self.port <= 65535:
            raise ConfigError("Port must be between 1024 and 65535.")

    @property
    def origin(self) -> str:
        return f"http://127.0.0.1:{self.port}"

    @property
    def allowed_hosts(self) -> set[str]:
        hosts = {f"127.0.0.1:{self.port}", f"localhost:{self.port}"}
        if self.dev:
            hosts |= {f"127.0.0.1:{VITE_DEV_PORT}", f"localhost:{VITE_DEV_PORT}"}
        return hosts

    @property
    def allowed_origins(self) -> set[str]:
        return {f"http://{host}" for host in self.allowed_hosts}

    @property
    def db_path(self) -> Path:
        return self.data_dir / "foundry-eval.sqlite3"

    @property
    def log_dir(self) -> Path:
        return self.data_dir / "logs"

    @classmethod
    def from_env(cls, *, port: int | None = None, dev: bool | None = None, host: str = "127.0.0.1") -> AppConfig:
        env = os.environ
        repo_root = find_repo_root()
        data_dir = validate_data_dir(default_data_dir(env), repo_root, env)
        static_env = env.get("FME_STATIC_DIR")
        static_dir = Path(static_env) if static_env else default_static_dir(data_dir, repo_root)
        return cls(
            data_dir=data_dir,
            host=host,
            port=port or int(env.get("FME_PORT", DEFAULT_PORT)),
            dev=dev if dev is not None else env.get("FME_DEV") == "1",
            keyring_mode=env.get("FME_KEYRING") or None,
            retail_prices_url=env.get("FME_RETAIL_PRICES_URL") or DEFAULT_RETAIL_PRICES_URL,
            static_dir=static_dir,
            repo_root=repo_root,
        )
