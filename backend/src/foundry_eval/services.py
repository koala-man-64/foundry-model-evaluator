"""Wiring of repositories and services shared by the API, engine and CLI."""

from __future__ import annotations

from dataclasses import dataclass

import httpx2

from .analysis.comparisons import ComparisonService
from .analysis.projections import ProjectionService
from .config import AppConfig
from .credentials.store import CredentialStore, CredentialStoreError
from .db.database import Database
from .execution.engine import Engine
from .execution.runs import RunService
from .judging.service import JudgeService
from .pricing.retail import RetailPricesClient
from .pricing.service import PricingService
from .providers.base import ProviderAdapter
from .providers.registry import default_adapters
from .repos.action_keys import ActionKeyRepository
from .repos.endpoints import EndpointRepository
from .repos.evaluations import EvaluationRepository
from .repos.profiles import ProfileRepository
from .repos.rates import RateRepository
from .repos.rubrics import RubricRepository
from .repos.runs import RunRepository
from .repos.settings import SettingsRepository
from .repos.suites import SuiteRepository
from .types import ApiStyle


class UnavailableCredentialStore(CredentialStore):
    """Placeholder when no secure credential store exists: every key operation fails clearly."""

    def __init__(self, reason: str) -> None:
        super().__init__(backend=None)
        self.reason = reason

    @property
    def backend_name(self) -> str:
        return "unavailable"

    def set(self, credential_ref: str, api_key: str) -> None:
        raise CredentialStoreError(self.reason)

    def get(self, credential_ref: str) -> str | None:
        raise CredentialStoreError(self.reason)

    def has(self, credential_ref: str) -> bool:
        return False

    def delete(self, credential_ref: str) -> None:
        return None


@dataclass
class Services:
    config: AppConfig
    db: Database
    credentials: CredentialStore
    settings: SettingsRepository
    endpoints: EndpointRepository
    profiles: ProfileRepository
    rates: RateRepository
    runs: RunRepository
    pricing: PricingService
    run_service: RunService
    engine: Engine
    suites: SuiteRepository
    rubrics: RubricRepository
    evaluations: EvaluationRepository
    judge: JudgeService
    comparisons: ComparisonService
    projections: ProjectionService
    action_keys: ActionKeyRepository
    credential_error: str | None = None

    def recover_interrupted(self) -> int:
        """After a restart: mark unfinished attempts, runs and evaluations interrupted (never replayed)."""
        currency = self.settings.get().currency
        return self.runs.recover_interrupted(currency) + self.evaluations.recover_interrupted(currency)


def build_services(
    config: AppConfig,
    *,
    adapters: dict[ApiStyle, ProviderAdapter] | None = None,
    retail_transport: httpx2.AsyncBaseTransport | None = None,
    credentials: CredentialStore | None = None,
) -> Services:
    db = Database(config.db_path)
    db.migrate()
    credential_error: str | None = None
    if credentials is None:
        try:
            credentials = CredentialStore.from_environment(config.keyring_mode)
        except CredentialStoreError as exc:
            credential_error = str(exc)
            credentials = UnavailableCredentialStore(str(exc))
    settings = SettingsRepository(db)
    pricing = PricingService(db, RetailPricesClient(config.retail_prices_url, transport=retail_transport))
    engine = Engine(db, credentials, adapters or default_adapters(), settings)
    judge = JudgeService(db, credentials, pricing, engine, settings)
    engine.on_generation_finished = judge.auto_judge
    engine.spent_hooks.append(judge.spent)
    run_service = RunService(db, credentials, pricing)
    run_service.judge_config_validator = judge.validate_frozen_config
    return Services(
        config=config,
        db=db,
        credentials=credentials,
        settings=settings,
        endpoints=EndpointRepository(db),
        profiles=ProfileRepository(db),
        rates=RateRepository(db),
        runs=RunRepository(db),
        pricing=pricing,
        run_service=run_service,
        engine=engine,
        suites=SuiteRepository(db),
        rubrics=RubricRepository(db),
        evaluations=EvaluationRepository(db),
        judge=judge,
        comparisons=ComparisonService(db, judge),
        projections=ProjectionService(db, pricing),
        action_keys=ActionKeyRepository(db),
        credential_error=credential_error,
    )
