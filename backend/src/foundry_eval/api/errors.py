"""Map domain exceptions to HTTP responses (never leaking secrets)."""

from __future__ import annotations

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse

from ..credentials.redaction import sanitize_error_text
from ..credentials.store import CredentialStoreError
from ..execution.runs import RunValidationError
from ..grading.checks import PatternCheckerUnavailable
from ..instance_lock import InstanceLockError
from ..judging.service import JudgeValidationError
from ..pricing.retail import RetailPricesError
from ..providers.request_builder import SettingsValidationError
from ..repos.endpoints import ConflictError, NotFoundError


def install_error_handlers(app: FastAPI) -> None:
    @app.exception_handler(NotFoundError)
    async def not_found(_: Request, exc: NotFoundError) -> JSONResponse:
        return JSONResponse({"detail": str(exc)}, status_code=404)

    @app.exception_handler(ConflictError)
    async def conflict(_: Request, exc: ConflictError) -> JSONResponse:
        return JSONResponse({"detail": str(exc)}, status_code=409)

    @app.exception_handler(SettingsValidationError)
    async def settings_invalid(_: Request, exc: SettingsValidationError) -> JSONResponse:
        return JSONResponse({"detail": str(exc), "issues": [i.model_dump() for i in exc.issues]}, status_code=422)

    @app.exception_handler(RunValidationError)
    async def run_invalid(_: Request, exc: RunValidationError) -> JSONResponse:
        return JSONResponse({"detail": str(exc), "preview": exc.preview.model_dump(mode="json")}, status_code=422)

    @app.exception_handler(JudgeValidationError)
    async def judge_invalid(_: Request, exc: JudgeValidationError) -> JSONResponse:
        return JSONResponse({"detail": str(exc), "preview": exc.preview.model_dump(mode="json")}, status_code=422)

    @app.exception_handler(CredentialStoreError)
    async def credentials_unavailable(_: Request, exc: CredentialStoreError) -> JSONResponse:
        return JSONResponse({"detail": sanitize_error_text(str(exc))}, status_code=503)

    @app.exception_handler(RetailPricesError)
    async def retail_failed(_: Request, exc: RetailPricesError) -> JSONResponse:
        return JSONResponse({"detail": sanitize_error_text(str(exc))}, status_code=502)

    @app.exception_handler(PatternCheckerUnavailable)
    async def pattern_checker_unavailable(_: Request, exc: PatternCheckerUnavailable) -> JSONResponse:
        detail = f"Pattern checks could not be vetted: {exc}. Try again shortly."
        return JSONResponse({"detail": detail}, status_code=503, headers={"Retry-After": "5"})

    @app.exception_handler(InstanceLockError)
    async def locked(_: Request, exc: InstanceLockError) -> JSONResponse:
        return JSONResponse({"detail": str(exc)}, status_code=409)

    @app.exception_handler(ValueError)
    async def bad_value(_: Request, exc: ValueError) -> JSONResponse:
        return JSONResponse({"detail": sanitize_error_text(str(exc))}, status_code=400)
