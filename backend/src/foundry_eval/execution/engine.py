"""Execution engine: bounded concurrency, isolation of failures, cancellation, no automatic retries.

Each attempt runs as its own task that records its own outcome, so one model's failure never
affects another's results. Attempts are never retried automatically; explicit retries create new
attempt records. Database writes are short and run on the event loop thread.
"""

from __future__ import annotations

import asyncio
import dataclasses
import functools
import logging
from collections import defaultdict
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from decimal import Decimal
from typing import Any

from ..credentials.redaction import sanitize_error_text
from ..credentials.store import CredentialStore
from ..db.database import Database
from ..domain.profiles import Endpoint, ModelProfile, host_identity
from ..money import add
from ..pricing.calculator import CostContext, CostEstimate, estimate_cost
from ..pricing.ratecard import RateCard
from ..pricing.snapshots import load_snapshot
from ..providers.base import ProviderAdapter, ProviderCall, ProviderResult, unusable_answer
from ..providers.request_builder import endpoint_url, profile_timeouts
from ..repos.endpoints import EndpointRepository, NotFoundError
from ..repos.runs import RunRepository
from ..repos.settings import SettingsRepository
from ..types import ApiStyle, AttemptStatus, RunStatus
from ..usage.normalizers import normalize_usage
from .budget import BudgetState, body_input_upper_bound, worst_case_cost

log = logging.getLogger(__name__)

RunHook = Callable[[str], Awaitable[None]]


def sent_cost(request_sent_at: str | None, cost: CostEstimate | None, reserved: Decimal | None) -> Decimal:
    """What a call counts against a budget: nothing if never sent, else its upper bound or reservation."""
    if request_sent_at is None:
        return Decimal(0)
    if cost is not None and cost.upper is not None:
        return cost.upper
    if reserved is not None:
        return reserved
    return cost.lower if cost is not None else Decimal(0)


@dataclass(frozen=True)
class NotSent:
    """A paid call that was not sent (cancelled, over budget, or the endpoint/key is unusable)."""

    status: AttemptStatus
    message: str


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
        # Work that belongs to a run but does not decide its status (judge evaluations).
        self._side_tasks: dict[str, set[asyncio.Task[None]]] = defaultdict(set)
        self._cancelled: set[str] = set()
        self._budgets: dict[str, BudgetState] = {}
        self._semaphore: asyncio.Semaphore | None = None
        self._semaphore_size = 0
        self._shutting_down = False
        # Called after a run's generation attempts finish (automatic judging).
        self.on_generation_finished: RunHook | None = None
        # Extra spending recorded against a run (judge evaluations), for budget accounting.
        self.spent_hooks: list[Callable[[str], Decimal]] = []
        self._after_generation_started: set[str] = set()
        self._loop: asyncio.AbstractEventLoop | None = None
        self.max_concurrent_observed = 0
        self._running = 0
        # Temporary limit that is not saved to the settings (the CLI smoke test runs one call at a time).
        self.concurrency_override: int | None = None
        # Work ids whose request has been sent (their budget reservation is spent unless settled).
        self._sent: set[str] = set()

    # ------------------------------------------------------------------ public API
    def is_active(self, run_id: str) -> bool:
        return bool(self._tasks.get(run_id)) or bool(self._side_tasks.get(run_id))

    def generation_active(self, run_id: str) -> bool:
        return bool(self._tasks.get(run_id))

    def semaphore(self) -> asyncio.Semaphore:
        size = self.concurrency_override or self.settings.get().concurrency
        idle = not any(self._tasks.values()) and not any(self._side_tasks.values())
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

    def track_side(self, run_id: str, coro: Awaitable[None], name: str) -> None:
        """Run extra work for a run (judge calls). It can be cancelled with the run but never sets its status."""
        try:
            asyncio.get_running_loop()
        except RuntimeError:
            if self._loop is None:
                raise RuntimeError("the execution engine is not bound to an event loop") from None
            future = asyncio.run_coroutine_threadsafe(self._track_side_async(run_id, coro, name), self._loop)
            future.result(timeout=30)
            return
        self._track_side_in_loop(run_id, coro, name)

    async def _track_side_async(self, run_id: str, coro: Awaitable[None], name: str) -> None:
        self._track_side_in_loop(run_id, coro, name)

    def _track_side_in_loop(self, run_id: str, coro: Awaitable[None], name: str) -> None:
        if not self._tasks.get(run_id):
            self._cancelled.discard(run_id)  # new work started explicitly after an earlier cancellation
        task = asyncio.ensure_future(coro)
        task.set_name(name)
        self._side_tasks[run_id].add(task)
        task.add_done_callback(functools.partial(self._side_task_done, run_id))

    async def cancel(self, run_id: str) -> None:
        self._cancelled.add(run_id)
        tasks = [*self._tasks.get(run_id, ()), *self._side_tasks.get(run_id, ())]
        for task in tasks:
            task.cancel()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)

    def is_cancelled(self, run_id: str) -> bool:
        return run_id in self._cancelled

    async def shutdown(self) -> None:
        self._shutting_down = True
        tasks = [t for groups in (self._tasks, self._side_tasks) for group in groups.values() for t in group]
        for task in tasks:
            task.cancel()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)

    @property
    def shutting_down(self) -> bool:
        return self._shutting_down

    async def wait(self, run_id: str) -> None:
        """Wait until the run's work, including follow-up work such as automatic judging, has finished."""
        while self._tasks.get(run_id) or self._side_tasks.get(run_id):
            pending = [*self._tasks.get(run_id, ()), *self._side_tasks.get(run_id, ())]
            await asyncio.gather(*pending, return_exceptions=True)
            # gather() completes without yielding when every task is already done, but the done
            # callbacks that remove finished tasks (and start follow-up work) only run when the loop
            # gets control. Always yield, or this loop could spin forever.
            await asyncio.sleep(0)

    def budget(self, run_id: str) -> BudgetState | None:
        return self._budgets.get(run_id)

    def committed(self, run_id: str) -> Decimal:
        """Spending counted against the run's budget. Read-only: never creates the budget state."""
        state = self._budgets.get(run_id)
        return state.committed if state is not None else self._spent(run_id)

    def ensure_budget(self, run_id: str) -> BudgetState | None:
        """The run's budget state, rebuilt from recorded spending when needed (e.g. after a restart)."""
        state = self._budgets.get(run_id)
        if state is None:
            run = self.runs.get(run_id)
            if run.budget is not None:
                state = self._budgets[run_id] = BudgetState(limit=run.budget, committed=self._spent(run_id))
        return state

    def settle(self, run_id: str, work_id: str, cost: CostEstimate) -> None:
        budget = self._budgets.get(run_id)
        if budget is not None:
            budget.settle(work_id, cost)

    def finish_budget(self, run_id: str, work_id: str) -> None:
        """Release an unused reservation; a sent call that was never settled keeps its worst case."""
        sent = work_id in self._sent
        self._sent.discard(work_id)
        budget = self._budgets.get(run_id)
        if budget is None:
            return
        if sent:
            budget.commit_reserved(work_id)
        else:
            budget.release(work_id)

    async def send(
        self,
        run_id: str,
        work_id: str,
        *,
        profile: ModelProfile,
        endpoint_info: dict[str, Any],
        body: dict[str, Any],
        card: RateCard,
        input_upper: int,
        max_output_tokens: int | None,
        mark_sent: Callable[[Decimal | None], None],
        semaphore: asyncio.Semaphore | None = None,
    ) -> ProviderResult | NotSent:
        """Send one paid call within the concurrency limit, the run budget and the endpoint checks.

        ``mark_sent`` is called right before the HTTP request with the budget reservation (if any),
        so an interruption afterwards is recorded as "sent" (possibly billed) and counted at its worst
        case. The API key is read here, just in time, and only sent to the host the run was created for.
        """
        async with semaphore or self.semaphore():
            self._running += 1
            self.max_concurrent_observed = max(self.max_concurrent_observed, self._running)
            try:
                if run_id in self._cancelled:
                    return NotSent(AttemptStatus.CANCELLED, "Cancelled before sending.")
                budget = self.ensure_budget(run_id)
                reserved: Decimal | None = None
                if budget is not None:
                    worst, reason = worst_case_cost(card, input_upper, max_output_tokens)
                    if worst is None or not budget.reserve(work_id, worst):
                        return NotSent(
                            AttemptStatus.SKIPPED_BUDGET,
                            reason or "Skipped: sending this call could exceed the run budget.",
                        )
                    reserved = worst
                try:
                    endpoint = self.endpoints.get(endpoint_info["id"])
                except NotFoundError:
                    return NotSent(AttemptStatus.FAILED, "The endpoint no longer exists.")
                if host_identity(endpoint.base_url) != host_identity(endpoint_info["base_url"]):
                    return NotSent(
                        AttemptStatus.FAILED,
                        "The endpoint URL changed after this work was created; start it again.",
                    )
                key = self.credentials.get(endpoint.credential_ref)
                if not key:
                    return NotSent(AttemptStatus.FAILED, f"No API key is stored for endpoint '{endpoint.name}'.")
                call_endpoint = Endpoint(**{**endpoint.model_dump(), "base_url": endpoint_info["base_url"]})
                url, query = endpoint_url(call_endpoint, profile)
                call = ProviderCall(
                    api_style=profile.api_style,
                    url=url,
                    query=query,
                    auth_style=endpoint.auth_style,
                    api_key=key,
                    body=body,
                    timeouts=profile_timeouts(profile),
                )
                adapter = self.adapters[profile.api_style]
                mark_sent(reserved)
                self._sent.add(work_id)
                return await adapter.execute(call)
            finally:
                self._running -= 1

    # ------------------------------------------------------------------ internals
    def _spent(self, run_id: str) -> Decimal:
        """Recorded spending for a run: every sent call at its upper bound, or its reserved worst case."""
        total = add(*(sent_cost(a.request_sent_at, a.cost, a.budget_reserved) for a in self.runs.attempts(run_id)))
        return add(total, *(hook(run_id) for hook in self.spent_hooks))

    def _task_done(self, run_id: str, task: asyncio.Task[None]) -> None:
        group = self._tasks.get(run_id)
        if group is not None:
            group.discard(task)
        if not task.cancelled() and task.exception() is not None:
            log.error("attempt task crashed", exc_info=task.exception())
        if group is not None and not group:
            del self._tasks[run_id]
            self._finalize(run_id)

    def _side_task_done(self, run_id: str, task: asyncio.Task[None]) -> None:
        group = self._side_tasks.get(run_id)
        if group is not None:
            group.discard(task)
            if not group:
                del self._side_tasks[run_id]
        if not task.cancelled() and task.exception() is not None:
            log.error("run task crashed", exc_info=task.exception())

    def _finalize(self, run_id: str) -> None:
        if self._shutting_down:
            return  # startup recovery marks unfinished work as interrupted
        if run_id in self._cancelled:
            self.runs.finish_run(run_id, RunStatus.CANCELLED)
            return
        counts = self.runs.status_counts(run_id)
        succeeded = counts.get(AttemptStatus.SUCCEEDED.value, 0)
        total = sum(counts.values())
        status = RunStatus.COMPLETED if succeeded == total else RunStatus.COMPLETED_WITH_ERRORS
        self.runs.finish_run(run_id, status)
        hook = self.on_generation_finished
        if hook is not None and run_id not in self._after_generation_started:
            self._after_generation_started.add(run_id)
            self._track_side_in_loop(run_id, hook(run_id), f"after-generation-{run_id}")

    async def _run_attempt(self, run_id: str, attempt_id: str, semaphore: asyncio.Semaphore) -> None:
        try:
            await self._execute(run_id, attempt_id, semaphore)
        except asyncio.CancelledError:
            status = AttemptStatus.INTERRUPTED if self._shutting_down else AttemptStatus.CANCELLED
            message = (
                "The app stopped before this attempt finished; it was not replayed."
                if self._shutting_down
                else "Cancelled. If the request had already been sent, the provider may still bill it."
            )
            self._finish_if_active(attempt_id, status, message)
            raise
        except Exception as exc:
            log.exception("attempt %s failed unexpectedly", attempt_id)
            self._finish_if_active(
                attempt_id, AttemptStatus.FAILED, sanitize_error_text(f"Internal error: {exc}") or ""
            )
        finally:
            self.finish_budget(run_id, attempt_id)

    def _finish_if_active(self, attempt_id: str, status: AttemptStatus, message: str) -> None:
        try:
            attempt = self.runs.attempt(attempt_id)
        except NotFoundError:
            return
        if attempt.status in (AttemptStatus.QUEUED, AttemptStatus.RUNNING):
            self.runs.finish_unsent_or_sent(attempt_id, status=status, message=message)

    async def _execute(self, run_id: str, attempt_id: str, semaphore: asyncio.Semaphore) -> None:
        attempt = self.runs.attempt(attempt_id)
        if attempt.status is not AttemptStatus.QUEUED:
            return
        candidate = self.runs.candidate(attempt.candidate_id)
        with self.db.connect() as conn:
            card, _meta = load_snapshot(conn, candidate.price_snapshot_id)
        currency = card.currency
        snapshot = dict(candidate.profile_snapshot)
        endpoint_info = snapshot.pop("endpoint")
        profile = ModelProfile.model_validate(snapshot)
        outcome = await self.send(
            run_id,
            attempt_id,
            profile=profile,
            endpoint_info=endpoint_info,
            body=attempt.request_body,
            card=card,
            # The stored body is exactly what is sent (prompt, settings, any schema), so it bounds the input.
            input_upper=body_input_upper_bound(attempt.request_body),
            max_output_tokens=candidate.settings.max_output_tokens,
            mark_sent=functools.partial(self.runs.mark_sent, attempt_id),
            semaphore=semaphore,
        )
        if isinstance(outcome, NotSent):
            self.runs.finish_not_sent(attempt_id, outcome.status, outcome.message, currency)
            return
        result = outcome
        problem = unusable_answer(result)
        if problem is not None:
            # Billed but not an answer: keep usage and cost, record it as a failure (never matched or judged).
            result = dataclasses.replace(result, ok=False, error_type="no_answer", error_message=problem)
        facts = normalize_usage(profile.api_style, result.raw_usage, service_tier=result.service_tier)
        if facts is None:
            reason = "usage not reported by the provider"
            if not result.ok:
                reason += f" (request failed: {result.error_type or 'error'}); no usage was reported"
            cost = CostEstimate.usage_missing(currency, reason, self.runs.attempt(attempt_id).budget_reserved)
        else:
            cost = estimate_cost(facts, card, CostContext(requested_cache_ttl=candidate.settings.prompt_cache_ttl))
        status = AttemptStatus.SUCCEEDED if result.ok else AttemptStatus.FAILED
        self.runs.finish_attempt(attempt_id, status=status, result=result, usage=facts, cost=cost)
        self.settle(run_id, attempt_id, cost)
