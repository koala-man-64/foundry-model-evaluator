"""FastAPI application factory: one loopback origin for the SPA and the local API."""

from __future__ import annotations

import asyncio
import logging
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from pathlib import Path

import httpx2
from fastapi import FastAPI, HTTPException
from fastapi.responses import FileResponse, HTMLResponse
from fastapi.staticfiles import StaticFiles

from .. import __version__
from ..config import AppConfig
from ..credentials.store import CredentialStore
from ..instance_lock import InstanceLock
from ..paths import ensure_data_dir
from ..providers.base import ProviderAdapter
from ..services import Services, build_services
from ..types import ApiStyle
from .errors import install_error_handlers
from .guard import HttpGuard
from .routes import analysis, endpoints, judging, pricing, profiles, runs, suites, system

log = logging.getLogger(__name__)

_NOT_BUILT = """<!doctype html><html lang="en"><head><meta charset="utf-8"><title>Foundry Model Evaluator</title></head>
<body><h1>Foundry Model Evaluator</h1><p>The web interface has not been built yet.</p>
<p>Run <code>npm ci</code> and <code>npm run build</code> in the <code>frontend</code> folder,
or use <code>scripts\\setup.ps1</code>, then restart the app.</p></body></html>"""


def create_app(
    config: AppConfig,
    *,
    adapters: dict[ApiStyle, ProviderAdapter] | None = None,
    retail_transport: httpx2.AsyncBaseTransport | None = None,
    credentials: CredentialStore | None = None,
    extra_setup: list[object] | None = None,
) -> FastAPI:
    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        ensure_data_dir(config.data_dir)
        lock = InstanceLock(config.data_dir / "instance.lock")
        lock.acquire()  # before recovery: a second instance must not touch the running one's work
        services: Services | None = None
        try:
            services = build_services(
                config, adapters=adapters, retail_transport=retail_transport, credentials=credentials
            )
            recovered = services.recover_interrupted()
            if recovered:
                log.warning("Marked %d unfinished calls from a previous session as interrupted.", recovered)
            services.engine.bind_loop(asyncio.get_running_loop())
            for setup in extra_setup or []:
                setup(services)  # type: ignore[operator]
            app.state.services = services
            yield
        finally:
            if services is not None:
                await services.engine.shutdown()
            app.state.services = None
            lock.release()

    app = FastAPI(
        title="Foundry Model Evaluator",
        version=__version__,
        lifespan=lifespan,
        docs_url=None,
        redoc_url=None,
        openapi_url="/api/openapi.json",
    )
    app.state.config = config
    install_error_handlers(app)
    for module in (system, endpoints, profiles, pricing, runs, suites, judging, analysis):
        app.include_router(module.router)
    _mount_frontend(app, config.static_dir)
    app.add_middleware(HttpGuard, allowed_hosts=config.allowed_hosts, allowed_origins=config.allowed_origins)
    return app


def _mount_frontend(app: FastAPI, static_dir: Path | None) -> None:
    index = static_dir / "index.html" if static_dir else None
    if static_dir is None or index is None or not index.is_file():

        @app.get("/", include_in_schema=False)
        def not_built() -> HTMLResponse:
            return HTMLResponse(_NOT_BUILT)

        return
    root = static_dir.resolve()
    assets = root / "assets"
    if assets.is_dir():
        app.mount("/assets", StaticFiles(directory=assets), name="assets")

    @app.get("/{full_path:path}", include_in_schema=False)
    def spa(full_path: str) -> FileResponse:
        if full_path.startswith("api/") or full_path == "api":
            raise HTTPException(status_code=404, detail="Not found")
        candidate = (root / full_path).resolve()
        if full_path and candidate.is_file() and candidate.is_relative_to(root):
            return FileResponse(candidate)
        return FileResponse(root / "index.html")
