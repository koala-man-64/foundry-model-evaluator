"""Execution engine: bounded concurrency, isolation of failures, cancellation, no automatic retries.

Each attempt runs as its own task that records its own outcome, so one model's failure never
affects another's results. Attempts are never retried automatically; explicit retries create new
attempt records. Database writes are short and run on the event loop thread.
"""

from __future__ import annotations

import asyncio
import functools
import logging
from collections import defaultdict
from collections.abc import Awaitable, Callable
from decimal import Decimal

from ..credentials.redaction import sanitize_error_text
from ..credentials.store import CredentialStore
from ..db.database import Database
from ..domain.profiles import Endpoint, ModelProfile, host_identity
from ..pricing.calculator import CostContext, CostEstimate, estimate_cost
from ..pricing.snapshots import load_snapshot
from ..providers.base import ProviderAdapter, ProviderCall
from ..providers.request_builder import endpoint_url, profile_timeouts
from ..repos.endpoints import EndpointRepository, NotFoundError
from ..repos.runs import RunRepository
from ..repos.settings import SettingsRepository
from ..types import ApiStyle, AttemptStatus, RunStatus
from ..usage.normalizers import normalize_usage
from .budget import BudgetState, input_token_upper_bound, worst_case_cost

log = logging.getLogger(__name__)

RunHook = Callable[[str], Awaitable[None]]


class Engine:
    def __init__(
        self,
        db: Database,
        credentials: CredentialStore,
        adapters: dict[ApiStyle, ProviderAdapter],
        settings: SettingsRepository,
    ) -> None:
        self.db = db
        self.credentials = credentials
        self.adapters = adapters
        self.settings = settings
        self.runs = RunRepository(db)
        self.endpoints = EndpointRepository(db)
        self._tasks: dict[str, set[asyncio.Task[None]]] = defaultdict(set)
        self._cancelled: set[str] = set()
        self._budgets: dict[str, BudgetState] = {}
        self._semaphore: asyncio.Semaphore | None = None
        self._semaphore_size = 0
        self._shutting_down = False
        # Called after a run's generation attempts finish (increment 2: automatic judging).
        self.on_generation_finished: RunHook | None = None
        self._after_generation_started: set[str] = set()
        self._loop: asyncio.AbstractEventLoop | None = None
        self.max_concurrent_observed = 0
        self._running = 0

    # ------------------------------------------------------------------ public API
    def is_active(self, run_id: str) -> bool:
        return bool(self._tasks.get(run_id))

    def semaphore(self) -> asyncio.Semaphore:
        size = self.settings.get().concurrency
        idle = not any(self._tasks.values())
        if self._semaphore is None or (idle and size != self._semaphore_size):
            self._semaphore = asyncio.Semaphore(size)
            self._semaphore_size = size
        return self._semaphore

    def bind_loop(self, loop: asyncio.AbstractEventLoop) -> None:
        self._loop = loop

    def start(self, run_id: str, attempt_ids: list[str] | None = None) -> None:
        """Schedule queued attempts of a run (or the given ones). Safe to call from any thread."""
        try:
            asyncio.get_running_loop()
        except RuntimeError:
            if self._loop is None:
                raise RuntimeError("the execution engine is not bound to an event loop") from None
            future = asyncio.run_coroutine_threadsafe(self._start_async(run_id, attempt_ids), self._loop)
            future.result(timeout=30)
            return
        self._start_in_loop(run_id, attempt_ids)

    async def _start_async(self, run_id: str, attempt_ids: list[str] | None) -> None:
        self._start_in_loop(run_id, attempt_ids)

    def _start_in_loop(self, run_id: str, attempt_ids: list[str] | None) -> None:
        run = self.runs.get(run_id)
        self._cancelled.discard(run_id)
        if run.budget is not None and run_id not in self._budgets:
            self._budgets[run_id] = BudgetState(limit=run.budget, committed=self._spent(run_id))
        if attempt_ids is None:
            attempt_ids = [a.id for a in self.runs.attempts(run_id) if a.status is AttemptStatus.QUEUED]
        if not attempt_ids:
            return
        self.runs.mark_run_started(run_id)
        semaphore = self.semaphore()
        for attempt_id in attempt_ids:
            task = asyncio.create_task(self._run_attempt(run_id, attempt_id, semaphore), name=f"attempt-{attempt_id}")
            self._tasks[run_id].add(task)
            task.add_done_callback(functools.partial(self._task_done, run_id))

    def track(self, run_id: str, coro: Awaitable[None], name: str) -> None:
        """Track additional run work (e.g. judge calls) so run completion waits for it."""
        task = asyncio.ensure_future(coro)
        task.set_name(name)
        self._tasks[run_id].add(task)
        task.add_done_callback(functools.partial(self._task_done, run_id))

    async def cancel(self, run_id: str) -> None:
        self._cancelled.add(run_id)
        tasks = list(self._tasks.get(run_id, ()))
        for task in tasks:
            task.cancel()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)

    def is_cancelled(self, run_id: str) -> bool:
        return run_id in self._cancelled

    async def shutdown(self) -> None:
        self._shutting_down = True
        tasks = [t for group in self._tasks.values() for t in group]
        for task in tasks:
            task.cancel()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)

    async def wait(self, run_id: str) -> None:
        while self._tasks.get(run_id):
            await asyncio.gather(*list(self._tasks[run_id]), return_exceptions=True)

    def budget(self, run_id: str) -> BudgetState | None:
        return self._budgets.get(run_id)

    # ------------------------------------------------------------------ internals
    def _spent(self, run_id: str) -> Decimal:
        total = Decimal(0)
        for attempt in self.runs.attempts(run_id):
            if attempt.cost is not None and attempt.cost.status != "not_sent":
                total += attempt.cost.upper if attempt.cost.upper is not None else attempt.cost.lower
        return total

    def _task_done(self, run_id: str, task: asyncio.Task[None]) -> None:
        group = self._tasks.get(run_id)
        if group is not None:
            group.discard(task)
        if not task.cancelled() and task.exception() is not None:
            log.error("attempt task crashed", exc_info=task.exception())
        if group is not None and not group:
            del self._tasks[run_id]
            self._finalize(run_id)

    def _finalize(self, run_id: str) -> None:
        if self._shutting_down:
            return  # startup recovery marks unfinished work as interrupted
        if run_id in self._cancelled:
            self.runs.finish_run(run_id, RunStatus.CANCELLED)
            return
        hook = self.on_generation_finished
        counts = self.runs.status_counts(run_id)
        if hook is not None and run_id not in self._after_generation_started:
            self._after_generation_started.add(run_id)
            self.track(run_id, hook(run_id), f"after-generation-{run_id}")
            return
        succeeded = counts.get(AttemptStatus.SUCCEEDED.value, 0)
        total = sum(counts.values())
        status = RunStatus.COMPLETED if succeeded == total else RunStatus.COMPLETED_WITH_ERRORS
        self.runs.finish_run(run_id, status)

    async def _run_attempt(self, run_id: str, attempt_id: str, semaphore: asyncio.Semaphore) -> None:
        currency = self.settings.get().currency
        try:
            await self._execute(run_id, attempt_id, semaphore)
        except asyncio.CancelledError:
            status = AttemptStatus.INTERRUPTED if self._shutting_down else AttemptStatus.CANCELLED
            message = (
                "The app stopped before this attempt finished; it was not replayed."
                if self._shutting_down
                else "Cancelled. If the request had already been sent, the provider may still bill it."
            )
            self._finish_if_active(attempt_id, status, message, currency)
            raise
        except Exception as exc:
            log.exception("attempt %s failed unexpectedly", attempt_id)
            self._finish_if_active(
                attempt_id, AttemptStatus.FAILED, sanitize_error_text(f"Internal error: {exc}") or "", currency
            )
        finally:
            budget = self._budgets.get(run_id)
            if budget is not None:
                budget.release(attempt_id)

    def _finish_if_active(self, attempt_id: str, status: AttemptStatus, message: str, currency: str) -> None:
        try:
            attempt = self.runs.attempt(attempt_id)
        except NotFoundError:
            return
        if attempt.status in (AttemptStatus.QUEUED, AttemptStatus.RUNNING):
            self.runs.finish_unsent_or_sent(attempt_id, status=status, message=message, currency=currency)

    async def _execute(self, run_id: str, attempt_id: str, semaphore: asyncio.Semaphore) -> None:
        attempt = self.runs.attempt(attempt_id)
        if attempt.status is not AttemptStatus.QUEUED:
            return
        candidate = self.runs.candidate(attempt.candidate_id)
        prompt = self.runs.prompt(attempt.prompt_id)
        with self.db.connect() as conn:
            card, _meta = load_snapshot(conn, candidate.price_snapshot_id)
        currency = card.currency
        snapshot = dict(candidate.profile_snapshot)
        endpoint_info = snapshot.pop("endpoint")
        profile = ModelProfile.model_validate(snapshot)
        async with semaphore:
            self._running += 1
            self.max_concurrent_observed = max(self.max_concurrent_observed, self._running)
            try:
                if run_id in self._cancelled:
                    message = "Cancelled before sending."
                    self.runs.finish_not_sent(attempt_id, AttemptStatus.CANCELLED, message, currency)
                    return
                budget = self._budgets.get(run_id)
                if budget is not None:
                    upper = input_token_upper_bound(prompt.system_text, prompt.user_text)
                    worst, reason = worst_case_cost(card, upper, candidate.settings.max_output_tokens)
                    if worst is None or not budget.reserve(attempt_id, worst):
                        message = reason or "Skipped: sending this call could exceed the run budget."
                        self.runs.finish_not_sent(attempt_id, AttemptStatus.SKIPPED_BUDGET, message, currency)
                        return
                try:
                    endpoint = self.endpoints.get(endpoint_info["id"])
                except NotFoundError:
                    self.runs.finish_not_sent(
                        attempt_id, AttemptStatus.FAILED, "The endpoint no longer exists.", currency
                    )
                    return
                if host_identity(endpoint.base_url) != host_identity(endpoint_info["base_url"]):
                    self.runs.finish_not_sent(
                        attempt_id,
                        AttemptStatus.FAILED,
                        "The endpoint URL changed after this run was created; create a new run.",
                        currency,
                    )
                    return
                key = self.credentials.get(endpoint.credential_ref)
                if not key:
                    self.runs.finish_not_sent(
                        attempt_id,
                        AttemptStatus.FAILED,
                        f"No API key is stored for endpoint '{endpoint.name}'.",
                        currency,
                    )
                    return
                call_endpoint = Endpoint(**{**endpoint.model_dump(), "base_url": endpoint_info["base_url"]})
                url, query = endpoint_url(call_endpoint, profile)
                call = ProviderCall(
                    api_style=profile.api_style,
                    url=url,
                    query=query,
                    auth_style=endpoint.auth_style,
                    api_key=key,
                    body=attempt.request_body,
                    timeouts=profile_timeouts(profile),
                )
                adapter = self.adapters[profile.api_style]
                self.runs.mark_sent(attempt_id)
                result = await adapter.execute(call)
            finally:
                self._running -= 1
        facts = normalize_usage(profile.api_style, result.raw_usage, service_tier=result.service_tier)
        if facts is None:
            reason = "usage not reported by the provider"
            if not result.ok:
                reason += f" (request failed: {result.error_type or 'error'}); no usage was reported"
            cost = CostEstimate.usage_missing(currency, reason)
        else:
            cost = estimate_cost(facts, card, CostContext(requested_cache_ttl=candidate.settings.prompt_cache_ttl))
        status = AttemptStatus.SUCCEEDED if result.ok else AttemptStatus.FAILED
        self.runs.finish_attempt(attempt_id, status=status, result=result, usage=facts, cost=cost)
        budget = self._budgets.get(run_id)
        if budget is not None:
            budget.settle(attempt_id, cost)
