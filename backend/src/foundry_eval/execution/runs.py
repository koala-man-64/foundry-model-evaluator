"""Run creation: validation, preview, price snapshots and queued attempts."""

from __future__ import annotations

import hashlib
from decimal import Decimal
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field

from ..credentials.store import CredentialStore
from ..db.database import Database
from ..domain.generation import GenerationSettings, SettingIssue
from ..domain.profiles import Endpoint, ModelProfile
from ..money import Money, add, mul, to_text
from ..pricing.ratecard import CATEGORY_LABELS, RateStatus
from ..pricing.service import PricingService
from ..pricing.snapshots import create_snapshot
from ..providers.base import Prompt
from ..providers.request_builder import SettingsValidationError, build_body
from ..repos.endpoints import EndpointRepository, NotFoundError
from ..repos.profiles import ProfileRepository
from ..repos.runs import RunRepository
from .budget import input_token_upper_bound, worst_case_cost


class PromptSpec(BaseModel):
    model_config = ConfigDict(extra="forbid")

    title: str = Field(default="", max_length=200)
    system: str = Field(default="", max_length=200_000)
    user: str = Field(min_length=1, max_length=400_000)
    suite_prompt_id: str | None = None


class CandidateSpec(BaseModel):
    model_config = ConfigDict(extra="forbid")

    profile_id: str
    settings: GenerationSettings = Field(default_factory=GenerationSettings)
    label: str | None = Field(default=None, max_length=120)


class RunRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    kind: Literal["compare", "suite", "smoke"] = "compare"
    title: str = Field(default="", max_length=200)
    prompts: list[PromptSpec] = Field(min_length=1, max_length=500)
    candidates: list[CandidateSpec] = Field(min_length=1, max_length=16)
    trials: int = Field(default=1, ge=1, le=50)
    budget: Money | None = None
    suite_id: str | None = None
    judge_config: dict[str, Any] | None = None


class CandidatePreview(BaseModel):
    index: int
    label: str
    profile_id: str
    profile_name: str | None
    issues: list[SettingIssue]
    key_configured: bool
    pricing_complete: bool
    unknown_rates: list[str]
    custom_rates: list[str]
    worst_case_per_call: Money | None
    worst_case_reason: str | None


class RunPreview(BaseModel):
    ok: bool
    calls: int
    errors: list[str]
    warnings: list[str]
    candidates: list[CandidatePreview]
    worst_case_total: Money | None
    budget: Money | None


class RunValidationError(ValueError):
    def __init__(self, preview: RunPreview) -> None:
        super().__init__("; ".join(preview.errors) or "invalid run")
        self.preview = preview


def content_hash(system: str, user: str) -> str:
    return hashlib.sha256(f"{system}\x00{user}".encode()).hexdigest()


def profile_snapshot(profile: ModelProfile, endpoint: Endpoint) -> dict[str, Any]:
    data = profile.model_dump(mode="json")
    data["endpoint"] = {
        "id": endpoint.id,
        "name": endpoint.name,
        "base_url": endpoint.base_url,
        "region": endpoint.region,
        "auth_style": endpoint.auth_style,
    }
    return data


def _settings_summary(settings: GenerationSettings, keys: set[str]) -> str:
    values = settings.provided()
    parts = [f"{k}={values[k]}" for k in sorted(keys) if k in values]
    return ", ".join(parts)


def candidate_labels(specs: list[CandidateSpec], profiles: dict[str, ModelProfile]) -> list[str]:
    labels: list[str] = []
    for spec in specs:
        if spec.label:
            labels.append(spec.label)
            continue
        profile = profiles.get(spec.profile_id)
        base = profile.name if profile else spec.profile_id
        siblings = [s for s in specs if s.profile_id == spec.profile_id]
        if len(siblings) > 1:
            varying: set[str] = set()
            all_keys = {k for s in siblings for k in s.settings.provided()}
            for key in all_keys:
                if len({repr(s.settings.provided().get(key)) for s in siblings}) > 1:
                    varying.add(key)
            summary = _settings_summary(spec.settings, varying)
            labels.append(f"{base} ({summary})" if summary else base)
        else:
            labels.append(base)
    seen: dict[str, int] = {}
    unique: list[str] = []
    for label in labels:
        seen[label] = seen.get(label, 0) + 1
        unique.append(label if seen[label] == 1 else f"{label} #{seen[label]}")
    return unique


class RunService:
    def __init__(self, db: Database, credentials: CredentialStore, pricing: PricingService) -> None:
        self.db = db
        self.credentials = credentials
        self.pricing = pricing
        self.runs = RunRepository(db)
        self.profiles = ProfileRepository(db)
        self.endpoints = EndpointRepository(db)

    def preview(self, request: RunRequest) -> RunPreview:
        errors: list[str] = []
        warnings: list[str] = []
        profiles: dict[str, ModelProfile] = {}
        endpoints: dict[str, Endpoint] = {}
        for spec in request.candidates:
            try:
                loaded = self.profiles.get(spec.profile_id)
                profiles[loaded.id] = loaded
                endpoints[loaded.id] = self.endpoints.get(loaded.endpoint_id)
            except NotFoundError:
                errors.append(f"Model profile {spec.profile_id} does not exist.")
        labels = candidate_labels(request.candidates, profiles)
        calls_per_candidate = len(request.prompts) * request.trials
        input_upper = max(input_token_upper_bound(p.system, p.user) for p in request.prompts)
        previews: list[CandidatePreview] = []
        worst_total: Decimal | None = Decimal(0)
        for index, spec in enumerate(request.candidates):
            profile = profiles.get(spec.profile_id)
            if profile is None:
                continue
            endpoint = endpoints[profile.id]
            issues: list[SettingIssue] = []
            if profile.archived:
                errors.append(f"{labels[index]}: the model profile is archived.")
            try:
                first = request.prompts[0]
                build_body(profile, spec.settings, Prompt(user=first.user, system=first.system))
            except SettingsValidationError as exc:
                issues = exc.issues
                errors.extend(f"{labels[index]}: {issue.message}" for issue in issues)
            key_configured = self.credentials.has(endpoint.credential_ref)
            if not key_configured:
                errors.append(f"{labels[index]}: no API key is stored for endpoint '{endpoint.name}'.")
            card = self.pricing.resolve(profile, endpoint)
            unknown: list[str] = []
            custom: list[str] = []
            for tier in card.tiers:
                for category, entry in tier.categories.items():
                    suffix = f" (> {tier.above_input_tokens} tokens)" if tier.above_input_tokens else ""
                    name = CATEGORY_LABELS[category] + suffix
                    if entry.status is RateStatus.UNKNOWN:
                        unknown.append(name)
                    elif entry.rate is not None and entry.rate.source.value == "custom_override":
                        custom.append(name)
            if unknown:
                warnings.append(
                    f"{labels[index]}: rates unknown for {', '.join(unknown)}; affected estimates will be incomplete."
                )
            worst, reason = worst_case_cost(card, input_upper, spec.settings.max_output_tokens)
            if worst is None or worst_total is None:
                worst_total = None
            else:
                worst_total = add(worst_total, mul(worst, Decimal(calls_per_candidate)))
            previews.append(
                CandidatePreview(
                    index=index,
                    label=labels[index],
                    profile_id=profile.id,
                    profile_name=profile.name,
                    issues=issues,
                    key_configured=key_configured,
                    pricing_complete=not unknown,
                    unknown_rates=unknown,
                    custom_rates=custom,
                    worst_case_per_call=worst,
                    worst_case_reason=reason,
                )
            )
        if request.budget is not None:
            if request.budget <= 0:
                errors.append("The budget must be greater than zero.")
            for item in previews:
                if item.worst_case_per_call is None:
                    errors.append(f"{item.label}: a budget needs a bounded cost per call ({item.worst_case_reason}).")
        if request.judge_config and request.kind != "suite":
            errors.append("Automatic judging can only be configured on suite runs.")
        calls = len(request.prompts) * len(request.candidates) * request.trials
        return RunPreview(
            ok=not errors,
            calls=calls,
            errors=errors,
            warnings=warnings,
            candidates=previews,
            worst_case_total=worst_total,
            budget=request.budget,
        )

    def create(self, request: RunRequest, idempotency_key: str) -> tuple[str, bool]:
        """Create a run with queued attempts. Returns (run_id, created)."""
        existing = self.runs.by_idempotency_key(idempotency_key)
        if existing is not None:
            return existing.id, False
        preview = self.preview(request)
        if not preview.ok:
            raise RunValidationError(preview)
        profiles = {s.profile_id: self.profiles.get(s.profile_id) for s in request.candidates}
        labels = candidate_labels(request.candidates, profiles)
        cards = {}
        for profile in profiles.values():
            endpoint = self.endpoints.get(profile.endpoint_id)
            cards[profile.id] = (endpoint, self.pricing.resolve(profile, endpoint))
        first_line = (request.prompts[0].user.strip().splitlines() or [""])[0][:80]
        title = request.title.strip() or request.prompts[0].title or first_line
        with self.db.transaction() as conn:
            run_id = self.runs.insert_run(
                conn,
                kind=request.kind,
                title=title,
                idempotency_key=idempotency_key,
                trials=request.trials,
                budget=to_text(request.budget) if request.budget is not None else None,
                suite_id=request.suite_id,
                judge_config=request.judge_config,
            )
            prompt_ids: list[tuple[str, PromptSpec]] = []
            for position, prompt_spec in enumerate(request.prompts):
                prompt_id = self.runs.insert_prompt(
                    conn,
                    run_id,
                    position,
                    title=prompt_spec.title,
                    system_text=prompt_spec.system,
                    user_text=prompt_spec.user,
                    content_hash=content_hash(prompt_spec.system, prompt_spec.user),
                    suite_prompt_id=prompt_spec.suite_prompt_id,
                )
                prompt_ids.append((prompt_id, prompt_spec))
            snapshot_ids: dict[str, str] = {}
            for position, spec in enumerate(request.candidates):
                profile = profiles[spec.profile_id]
                endpoint, card = cards[profile.id]
                if profile.id not in snapshot_ids:
                    snapshot_ids[profile.id] = create_snapshot(conn, profile, endpoint, card)
                identity = profile.pricing_identity(endpoint, card.currency)
                key = hashlib.sha256(f"{identity}|{spec.settings.canonical_json()}".encode()).hexdigest()[:16]
                candidate_id = self.runs.insert_candidate(
                    conn,
                    run_id,
                    position,
                    label=labels[position],
                    profile_id=profile.id,
                    profile_snapshot=profile_snapshot(profile, endpoint),
                    settings=spec.settings,
                    candidate_key=key,
                    price_snapshot_id=snapshot_ids[profile.id],
                )
                for prompt_id, stored_prompt in prompt_ids:
                    body = build_body(
                        profile, spec.settings, Prompt(user=stored_prompt.user, system=stored_prompt.system)
                    )
                    for trial in range(1, request.trials + 1):
                        self.runs.insert_attempt(
                            conn,
                            run_id=run_id,
                            candidate_id=candidate_id,
                            prompt_id=prompt_id,
                            trial_index=trial,
                            request_body=body,
                        )
        return run_id, True

    def create_retry(self, attempt_id: str) -> str:
        """Explicit retry: a new attempt record with the same candidate, prompt and request body."""
        original = self.runs.attempt(attempt_id)
        if original.status.value in ("queued", "running"):
            raise ValueError("This attempt is still in progress.")
        with self.db.transaction() as conn:
            return self.runs.insert_attempt(
                conn,
                run_id=original.run_id,
                candidate_id=original.candidate_id,
                prompt_id=original.prompt_id,
                trial_index=original.trial_index,
                request_body=original.request_body,
                retry_of=original.id,
            )
