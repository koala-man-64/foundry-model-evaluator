"""Model profiles and capability presets."""

from __future__ import annotations

from fastapi import APIRouter
from pydantic import BaseModel

from ...domain.capabilities import SETTING_KEYS, SETTING_KINDS, SETTING_LABELS, Capabilities
from ...domain.presets import PRESETS, preset_capabilities
from ...domain.profiles import ModelProfile, ProfileFields
from ...types import ApiStyle
from ..deps import ServicesDep

router = APIRouter(prefix="/api", tags=["profiles"])


class PresetOut(BaseModel):
    id: str
    label: str
    description: str
    api_styles: list[ApiStyle]
    capabilities: dict[str, Capabilities]
    rate_defaults: dict[str, str]


class SettingMeta(BaseModel):
    key: str
    label: str
    kind: str


class PresetCatalog(BaseModel):
    presets: list[PresetOut]
    settings: list[SettingMeta]


class ArchiveIn(BaseModel):
    archived: bool


@router.get("/capability-presets", response_model=PresetCatalog)
def list_presets() -> PresetCatalog:
    presets = [
        PresetOut(
            id=p.id,
            label=p.label,
            description=p.description,
            api_styles=list(p.api_styles),
            capabilities={style.value: preset_capabilities(p.id, style) for style in p.api_styles},
            rate_defaults={c.value: s for c, s in p.rate_defaults.items()},
        )
        for p in PRESETS.values()
    ]
    settings = [SettingMeta(key=k, label=SETTING_LABELS[k], kind=SETTING_KINDS[k]) for k in SETTING_KEYS]
    return PresetCatalog(presets=presets, settings=settings)


@router.get("/profiles", response_model=list[ModelProfile])
def list_profiles(services: ServicesDep, include_archived: bool = False) -> list[ModelProfile]:
    return services.profiles.list(include_archived=include_archived)


@router.post("/profiles", response_model=ModelProfile, status_code=201)
def create_profile(payload: ProfileFields, services: ServicesDep) -> ModelProfile:
    services.endpoints.get(payload.endpoint_id)
    profile = services.profiles.create(payload)
    services.pricing.match_and_apply(profile.id)
    return services.profiles.get(profile.id)


@router.get("/profiles/{profile_id}", response_model=ModelProfile)
def get_profile(profile_id: str, services: ServicesDep) -> ModelProfile:
    return services.profiles.get(profile_id)


@router.put("/profiles/{profile_id}", response_model=ModelProfile)
def update_profile(profile_id: str, payload: ProfileFields, services: ServicesDep) -> ModelProfile:
    services.endpoints.get(payload.endpoint_id)
    profile = services.profiles.update(profile_id, payload)
    services.pricing.match_and_apply(profile.id)
    return profile


@router.post("/profiles/{profile_id}/archive", response_model=ModelProfile)
def archive_profile(profile_id: str, payload: ArchiveIn, services: ServicesDep) -> ModelProfile:
    return services.profiles.set_archived(profile_id, payload.archived)
