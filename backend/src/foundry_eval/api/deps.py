"""FastAPI dependencies."""

from __future__ import annotations

from typing import Annotated

from fastapi import Depends, Header, HTTPException, Request

from ..services import Services


def get_services(request: Request) -> Services:
    services: Services | None = getattr(request.app.state, "services", None)
    if services is None:
        raise HTTPException(status_code=503, detail="The app is still starting.")
    return services


ServicesDep = Annotated[Services, Depends(get_services)]


def idempotency_key(idempotency_key: Annotated[str | None, Header(alias="Idempotency-Key")] = None) -> str:
    if not idempotency_key or not 8 <= len(idempotency_key) <= 128:
        raise HTTPException(status_code=400, detail="An Idempotency-Key header (8-128 characters) is required.")
    return idempotency_key


IdempotencyKey = Annotated[str, Depends(idempotency_key)]
