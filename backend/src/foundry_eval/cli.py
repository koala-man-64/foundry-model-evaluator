"""Command line entry point: ``foundry-eval serve | pricing-check | smoke | run-suite | export-openapi``."""

from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import logging
import socket
import sys
import threading
import time
import webbrowser
from decimal import Decimal
from pathlib import Path
from typing import TYPE_CHECKING, Any

from . import __version__
from .config import AppConfig, ConfigError
from .instance_lock import InstanceLock, InstanceLockError
from .logging_setup import configure_logging
from .paths import DataDirError, ensure_data_dir

if TYPE_CHECKING:
    from .app_client import AppClient
    from .gates import Gates


def _port_available(port: int) -> bool:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        if sys.platform == "win32":
            sock.setsockopt(socket.SOL_SOCKET, socket.SO_EXCLUSIVEADDRUSE, 1)
        else:
            # Ignore TIME_WAIT leftovers of a previous run; a live listener still makes bind fail.
            sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        try:
            sock.bind(("127.0.0.1", port))
        except OSError:
            return False
    return True


def cmd_serve(args: argparse.Namespace) -> int:
    import uvicorn

    from .api.app import create_app

    config = AppConfig.from_env(port=args.port, dev=args.dev)
    ensure_data_dir(config.data_dir)
    configure_logging(config.log_dir)
    if not _port_available(config.port):
        print(f"Port {config.port} is already in use. Is the app already running? Try {config.origin}", file=sys.stderr)
        return 2
    if config.static_dir is None or not (config.static_dir / "index.html").is_file():
        print("Note: the web interface is not built yet (run npm ci and npm run build in frontend).")
    print(f"Foundry Model Evaluator {__version__} — data in {config.data_dir}")
    print(f"Open {config.origin} (Ctrl+C to stop)")
    if not args.no_browser:
        threading.Timer(1.5, lambda: webbrowser.open(config.origin)).start()
    uvicorn.run(
        create_app(config),
        host=config.host,
        port=config.port,
        log_config=None,
        workers=1,
        reload=False,
        server_header=False,
        proxy_headers=False,
    )
    return 0


def cmd_export_openapi(args: argparse.Namespace) -> int:
    from .api.app import create_app

    config = AppConfig(data_dir=Path(args.output).resolve().parent / ".openapi-tmp", port=8765)
    schema = create_app(config).openapi()
    Path(args.output).write_text(json.dumps(schema, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    print(f"Wrote {args.output}")
    return 0


def _offline_services(config: AppConfig):  # type: ignore[no-untyped-def]
    from .services import build_services

    ensure_data_dir(config.data_dir)
    lock = InstanceLock(config.data_dir / "instance.lock")
    try:
        lock.acquire()
    except InstanceLockError:
        print("The app is running. Close it first (or use the Refresh/Run buttons in the app).", file=sys.stderr)
        raise
    return lock, build_services(config)


def cmd_pricing_check(args: argparse.Namespace) -> int:
    from .pricing.resolver import pricing_context

    config = AppConfig.from_env()
    configure_logging(None, logging.WARNING)
    lock, services = _offline_services(config)
    try:
        profiles = [p for p in services.profiles.list() if not args.profile or p.name in args.profile]
        if not profiles:
            print("No matching model profiles.", file=sys.stderr)
            return 1
        regions = {
            pricing_context(p, services.endpoints.get(p.endpoint_id), services.settings.get().currency).region
            for p in profiles
        }
        regions.discard("")
        print(f"Fetching live Azure Retail Prices for: {', '.join(sorted(regions)) or '(no regions set)'} …")
        for result in asyncio.run(services.pricing.refresh(regions)):
            error = f" — {result.error}" if result.error else ""
            print(f"  {result.region}: {result.status} ({result.item_count} items){error}")
            for warning in result.warnings:
                print(f"    warning: {warning}")
        for profile in profiles:
            terms = profile.meter_terms or "—"
            print(f"\n{profile.name}  (meter terms: {terms}, {profile.deployment_type}, {profile.processing_tier})")
            match = services.pricing.match_and_apply(profile.id)
            for m in match.matches:
                chosen = m.chosen.info.item if m.chosen else None
                detail = (
                    f"{chosen.meter_name} = {chosen.retail_price} per {chosen.unit_of_measure}" if chosen else m.message
                )
                print(f"  {m.category.value:<16} {m.context:<5} {m.status:<10} {detail}")
            for note in match.notes:
                print(f"  note: {note}")
            card = services.pricing.resolve(profile)
            for tier in card.tiers:
                for category, entry in tier.categories.items():
                    rate = entry.rate
                    shown = (
                        f"{rate.per_million} per 1M ({rate.source.value})"
                        if rate
                        else (entry.reason or entry.status.value)
                    )
                    print(f"    rate {category.value:<16} tier>{tier.above_input_tokens:<7} {shown}")
        return 0
    finally:
        lock.release()


def cmd_smoke(args: argparse.Namespace) -> int:
    from .domain.generation import GenerationSettings
    from .execution.engine import sent_cost
    from .execution.runs import CandidateSpec, PromptSpec, RunRequest, RunValidationError
    from .judging.service import JudgeRequest, JudgeValidationError
    from .money import add, validate_money_input
    from .timeutil import new_id

    try:
        budget = validate_money_input(args.budget)
    except ValueError:
        print("--budget must be a decimal amount such as 0.05", file=sys.stderr)
        return 2
    if budget <= 0:
        print("--budget must be greater than zero", file=sys.stderr)
        return 2
    config = AppConfig.from_env()
    configure_logging(config.log_dir, logging.WARNING)
    lock, services = _offline_services(config)
    try:
        by_name = {p.name: p for p in services.profiles.list()}
        missing = [name for name in [*args.profile, *([args.judge] if args.judge else [])] if name not in by_name]
        if missing:
            print(f"Unknown profiles: {', '.join(missing)}", file=sys.stderr)
            return 2
        candidates = [
            CandidateSpec(
                profile_id=by_name[name].id,
                settings=GenerationSettings(max_output_tokens=args.max_output_tokens),
            )
            for name in args.profile
        ]
        request = RunRequest(
            kind="smoke",
            title="Live smoke test",
            prompts=[PromptSpec(title="smoke", user=args.prompt)],
            candidates=candidates,
            budget=budget,
        )
        preview = services.run_service.preview(request)
        for warning in preview.warnings:
            print(f"warning: {warning}")
        print(f"Worst-case cost: {preview.worst_case_total} {services.settings.get().currency} (budget {budget})")
        try:
            run_id, _ = services.run_service.create(request, f"smoke-{new_id()}")
        except RunValidationError as exc:
            for error in exc.preview.errors:
                print(f"error: {error}", file=sys.stderr)
            return 2

        evaluation_ids: list[str] = []

        async def execute() -> None:
            services.engine.concurrency_override = 1  # one call at a time; the saved setting is unchanged
            services.engine.semaphore()  # bind to this loop
            services.engine.start(run_id)
            await services.engine.wait(run_id)
            if args.judge:
                judge_request = JudgeRequest(
                    judge_profile_id=by_name[args.judge].id,
                    settings=GenerationSettings(max_output_tokens=args.judge_max_output_tokens),
                )
                try:
                    ids, _ = services.judge.create(run_id, judge_request, f"smoke-judge-{new_id()}")
                except JudgeValidationError as exc:
                    for error in exc.preview.errors:
                        print(f"judge error: {error}", file=sys.stderr)
                    return
                evaluation_ids.extend(ids)
                await asyncio.gather(*(services.judge.run_evaluation(run_id, i) for i in ids))

        asyncio.run(execute())
        attempts = services.runs.attempts(run_id)
        spent = Decimal(0)
        failures = 0
        for attempt in attempts:
            candidate = services.runs.candidate(attempt.candidate_id)
            usage = attempt.usage
            cost = attempt.cost
            spent = add(spent, sent_cost(attempt.request_sent_at, cost, attempt.budget_reserved))
            if attempt.status.value != "succeeded":
                failures += 1
            print(f"\n{candidate.label}: {attempt.status.value} ({attempt.finish_reason}) in {attempt.latency_ms} ms")
            if attempt.error_message:
                print(f"  error: {attempt.error_message}")
            if usage:
                print(
                    f"  input {usage.input_total}  uncached {usage.input_uncached}  cache read {usage.cache_read}"
                    f"  cache write {usage.cache_write}  output {usage.output_total}  reasoning {usage.reasoning}"
                )
            if cost:
                total = cost.total if cost.total is not None else f">= {cost.lower} (incomplete)"
                print(f"  estimated token cost: {total}")
                for reason in cost.reasons:
                    print(f"    - {reason}")
        labels = {c.id: c.label for c in services.runs.candidates(run_id)}
        for evaluation_id in evaluation_ids:
            evaluation = services.evaluations.get(evaluation_id)
            cost = evaluation.cost
            spent = add(spent, sent_cost(evaluation.request_sent_at, cost, evaluation.budget_reserved))
            print(f"\nJudge ({args.judge}): {evaluation.status}")
            if evaluation.error_message:
                print(f"  {evaluation.error_message}")
            for error in evaluation.parse_errors:
                print(f"  - {error}")
            for item in evaluation.candidates:
                scores = ", ".join(f"{c.key} {c.score}" for c in item.criteria)
                label = labels.get(item.candidate_id, item.candidate_id)
                print(f"  {item.label} = {label}: weighted {item.weighted_overall} ({scores})")
            if evaluation.preferred:
                print(f"  preferred: {evaluation.preferred}")
            if cost:
                total = cost.total if cost.total is not None else f">= {cost.lower} (incomplete)"
                print(f"  estimated judging cost: {total}")
            if evaluation.status != "succeeded":
                failures += 1
        print(f"\nSpent (upper bound): {spent} of budget {budget}. Results are in the app's History (run {run_id}).")
        return 1 if failures else 0
    finally:
        lock.release()


class RunSuiteError(Exception):
    """Stops ``run-suite`` before a verdict (exit code 2)."""


def suite_run_key(suite_id: str, candidate_ids: list[str] | None, run_key: str) -> str:
    """The Idempotency-Key for ``--run-key``: the same suite, candidates and run key give the same run."""
    selection = ",".join(sorted(candidate_ids)) if candidate_ids else "*"
    digest = hashlib.sha256(f"run-suite\x00{suite_id}\x00{selection}\x00{run_key}".encode()).hexdigest()
    return f"cli-suite-{digest[:48]}"


def _app_client(config: AppConfig) -> AppClient:
    from .app_client import AppClient

    return AppClient(config.origin)


def _path_id(value: str) -> str:
    from urllib.parse import quote

    return quote(value, safe="")


def _resolve_suite(client: AppClient, wanted: str) -> dict[str, Any]:
    suites: list[dict[str, Any]] = client.get("/api/suites", {"include_archived": "true"})
    matches = [s for s in suites if s["id"] == wanted] or [s for s in suites if s["name"] == wanted]
    if not matches:
        raise RunSuiteError(f"No suite has the id or name {wanted!r}.")
    if len(matches) > 1:
        raise RunSuiteError(f"Several suites are named {wanted!r}; pass an id: {', '.join(s['id'] for s in matches)}.")
    suite = matches[0]
    if suite["archived"]:
        raise RunSuiteError(f"The suite {suite['name']!r} is archived; restore it to run it.")
    return suite


def _resolve_candidates(client: AppClient, suite: dict[str, Any], wanted: list[str]) -> list[str]:
    """Suite candidate ids for ``--candidate`` values: a candidate id, its label, or its profile's name."""
    profiles = {p["id"]: p["name"] for p in client.get("/api/profiles", {"include_archived": "true"})}
    candidates: list[dict[str, Any]] = suite["candidates"]
    ids: list[str] = []
    for name in wanted:
        matches = (
            [c for c in candidates if c["id"] == name]
            or [c for c in candidates if c["label"] == name]
            or [c for c in candidates if profiles.get(c["profile_id"]) == name]
        )
        if len(matches) != 1:
            problem = "matches several candidates" if matches else "is not a candidate of this suite"
            listing = "; ".join(
                f"{c['label'] or profiles.get(c['profile_id'], c['profile_id'])} [{c['id']}]"
                for c in (matches or candidates)
            )
            raise RunSuiteError(f"--candidate {name!r} {problem}. Choose by id: {listing}.")
        if matches[0]["id"] in ids:
            raise RunSuiteError(f"--candidate {name!r} chooses a candidate that is already chosen.")
        ids.append(matches[0]["id"])
    return ids


def _resolve_baseline(client: AppClient, run_id: str, wanted: str | None) -> tuple[dict[str, Any], dict[str, Any]]:
    """The baseline run and the candidate to compare against, checked before anything is spent."""
    from .app_client import ApiError

    try:
        detail: dict[str, Any] = client.get(f"/api/runs/{_path_id(run_id)}")
    except ApiError as exc:
        if exc.status == 404:
            raise RunSuiteError(f"The baseline run {run_id!r} does not exist.") from exc
        raise
    if detail["active"]:
        raise RunSuiteError("The baseline run is still running; wait until it has finished.")
    candidates = {c["candidate_key"]: c for c in detail["candidates"]}
    if wanted is None:
        matches = list(candidates.values())
    else:
        matches = [c for c in candidates.values() if wanted in (c["candidate_key"], c["label"])]
    if len(matches) != 1:
        listing = "; ".join(f"{c['label']} [{c['candidate_key']}]" for c in candidates.values())
        if wanted is None:
            problem = "The baseline run has several candidates; choose one with --baseline-candidate"
        else:
            problem = f"--baseline-candidate {wanted!r} matches {'several' if matches else 'no'} baseline candidates"
        raise RunSuiteError(f"{problem}: {listing}.")
    return detail, matches[0]


def _wait_for_run(client: AppClient, run_id: str, *, timeout: float, poll: float) -> None:
    """Wait until the run, including automatic judging, has finished. Progress goes to stderr."""
    deadline = time.monotonic() + timeout
    shown = ""
    while True:
        progress = client.get(f"/api/runs/{_path_id(run_id)}/progress")
        counts: dict[str, int] = progress["counts"]
        total: int = progress["total"]
        finished = total - counts.get("queued", 0) - counts.get("running", 0)
        line = f"{finished}/{total} attempts finished, {counts.get('succeeded', 0)} succeeded"
        if progress["active"] and finished == total:
            line += "; judging"
        if line != shown:
            print(f"run {run_id}: {line}", file=sys.stderr)
            shown = line
        if not progress["active"]:
            if progress["status"] == "queued":
                raise RunSuiteError("The run is queued, but the app is not running it; see the app's log file.")
            if progress["status"] != "running":
                return
        if time.monotonic() >= deadline:
            raise RunSuiteError(
                f"The run is still going after {timeout:g} s and continues in the app. Run the same command again "
                "(same --run-key) to keep waiting; nothing new is sent."
            )
        time.sleep(poll)


def _cost_fields(total: dict[str, Any]) -> dict[str, Any]:
    return {name: total.get(name) for name in ("status", "total", "lower", "upper", "counted", "incomplete", "pending")}


def _has_judging(detail: dict[str, Any]) -> bool:
    return bool(detail["run"].get("judge_config") or detail.get("evaluation_counts") or detail.get("judge_scores"))


def _has_checks(detail: dict[str, Any]) -> bool:
    return any(prompt.get("checks") for prompt in detail["prompts"])


def _check_baseline_prompts(suite: dict[str, Any], baseline: tuple[dict[str, Any], dict[str, Any]]) -> None:
    """Refuse, before anything is spent, a suite that shares no prompt with the baseline run."""
    from .execution.runs import content_hash

    detail, _ = baseline
    known = {prompt["content_hash"] for prompt in detail["prompts"]}
    shared = sum(1 for prompt in suite["prompts"] if content_hash(prompt["system"], prompt["user"]) in known)
    if not shared:
        raise RunSuiteError("None of the suite's prompts is in the baseline run, so nothing could be compared.")
    if shared < len(suite["prompts"]):
        print(
            f"warning: {shared} of the suite's {len(suite['prompts'])} prompts are in the baseline run; "
            "only those are compared",
            file=sys.stderr,
        )


def _run_suite(
    client: AppClient,
    config: AppConfig,
    args: argparse.Namespace,
    *,
    budget: Decimal,
    gates: Gates,
    metrics: tuple[str, ...],
    tolerances: dict[str, Decimal],
    report: dict[str, Any],
) -> None:
    from .app_client import ApiError
    from .gates import EXIT_CODES, baseline_gate, counted_attempts, overall_outcome, run_gates
    from .money import to_text

    suite = _resolve_suite(client, args.suite)
    report["suite"] = {"id": suite["id"], "name": suite["name"]}
    candidate_ids = _resolve_candidates(client, suite, args.candidate) if args.candidate else None
    if candidate_ids is not None and set(candidate_ids) == {c["id"] for c in suite["candidates"]}:
        candidate_ids = None  # every candidate: the same run (and run key) as naming none
    baseline = _resolve_baseline(client, args.baseline_run, args.baseline_candidate) if args.baseline_run else None
    if baseline is not None:
        _check_baseline_prompts(suite, baseline)
    title = args.title if args.title is not None else f"{suite['name']} ({args.run_key})"
    body: dict[str, Any] = {"title": title[:200], "budget": to_text(budget)}
    if candidate_ids is not None:
        body["candidate_ids"] = candidate_ids
    suite_path = f"/api/suites/{_path_id(suite['id'])}"

    preview = client.post(f"{suite_path}/preview", body)
    report["preview"] = {name: preview[name] for name in ("calls", "worst_case_total", "budget", "warnings", "errors")}
    for warning in preview["warnings"]:
        print(f"warning: {warning}", file=sys.stderr)
    if not preview["ok"]:
        raise RunSuiteError("The app refused the run: " + "; ".join(preview["errors"]))
    if baseline is not None:
        baseline_key = baseline[1]["candidate_key"]
        for item in preview["candidates"]:
            if item["candidate_key"] == baseline_key:
                raise RunSuiteError(
                    f"The candidate {item['label']!r} would be the baseline candidate again (same candidate key), "
                    "and comparisons would pool it into the frozen baseline. Choose the new candidates with "
                    "--candidate."
                )
    worst = preview["worst_case_total"]
    print(f"{preview['calls']} calls, worst-case cost {worst} (budget {to_text(budget)})", file=sys.stderr)
    if worst is not None and Decimal(worst) > budget:
        print(
            "warning: the worst case exceeds the budget; calls that no longer fit are skipped and count as failed",
            file=sys.stderr,
        )

    try:
        created = client.post(
            f"{suite_path}/runs", body, idempotency_key=suite_run_key(suite["id"], candidate_ids, args.run_key)
        )
    except ApiError as exc:
        if exc.status < 500:
            raise
        raise RunSuiteError(
            f"The app answered: {exc}. The run may have been created; run the same command again (same --run-key) "
            "to follow it without paying twice."
        ) from exc
    run_id: str = created["run_id"]
    report["run"] = {"id": run_id, "created": created["created"], "url": f"{config.origin}/runs/{run_id}"}
    if not created["created"]:
        print(f"This --run-key was used before: following run {run_id}; nothing new is sent.", file=sys.stderr)
    _wait_for_run(client, run_id, timeout=args.timeout, poll=args.poll_interval)

    detail: dict[str, Any] = client.get(f"/api/runs/{_path_id(run_id)}")
    run = detail["run"]
    if run["budget"] is not None and Decimal(run["budget"]) != budget:
        print(f"note: this run's budget is {run['budget']}, set when it was created", file=sys.stderr)
    attempts = counted_attempts(detail["attempts"])
    statuses: dict[str, int] = {}
    for attempt in attempts:
        statuses[attempt["status"]] = statuses.get(attempt["status"], 0) + 1
    report["run"].update(
        title=run["title"],
        status=run["status"],
        candidates=[
            {name: c[name] for name in ("label", "candidate_key", "profile_name", "deployment")}
            for c in detail["candidates"]
        ],
        attempts={"total": len(attempts), "by_status": dict(sorted(statuses.items()))},
        judging={"by_status": detail.get("evaluation_counts", {}), "error": run.get("judge_error")},
    )
    report["cost"] = {
        "currency": detail["combined_cost"]["currency"],
        **_cost_fields(detail["combined_cost"]),
        "generation": _cost_fields(detail["generation_cost"]),
        "judging": _cost_fields(detail["judging_cost"]),
        "budget": (detail["budget"] or {}).get("limit"),
        "budget_committed": (detail["budget"] or {}).get("committed"),
    }

    evaluations = client.get(f"/api/runs/{_path_id(run_id)}/evaluations") if _has_judging(detail) else []
    results = run_gates(gates, detail, evaluations)
    if baseline is not None:
        baseline_detail, baseline_candidate = baseline
        baseline_key = baseline_candidate["candidate_key"]
        keys = list(dict.fromkeys(c["candidate_key"] for c in detail["candidates"]))
        new_keys = [key for key in keys if key != baseline_key]
        params = [("run_id", baseline_detail["run"]["id"]), ("run_id", run_id)]
        params += [("candidate_key", key) for key in [baseline_key, *new_keys]] + [("baseline", baseline_key)]
        applies = {
            "judge": _has_judging(baseline_detail) or _has_judging(detail),
            "checks": _has_checks(baseline_detail) or _has_checks(detail),
            "cost": True,
        }
        comparison: dict[str, Any] = {"candidates": []}
        error = None
        try:
            comparison = client.get("/api/comparisons", params)
        except ApiError as exc:
            error = exc.detail
        results.append(
            baseline_gate(
                comparison,
                baseline_run_id=baseline_detail["run"]["id"],
                baseline_key=baseline_key,
                candidate_keys=new_keys,
                metrics=metrics,
                tolerances=tolerances,
                applies=applies,
                remeasured=baseline_key in keys,
                error=error,
            )
        )
    report["gates"] = results
    outcome = overall_outcome(results)
    report["outcome"] = outcome
    report["exit_code"] = EXIT_CODES[outcome]


def _print_summary(report: dict[str, Any]) -> None:
    for gate in report.get("gates", []):
        print(f"{gate['gate']}: {gate['outcome']} ({gate['detail']})", file=sys.stderr)
        for row in gate.get("candidates", []):
            for metric, result in row["metrics"].items():
                print(f"  {row['label']} {metric}: {result['status']} ({result['reason']})", file=sys.stderr)
    run = report.get("run", {})
    where = f" Run: {run['url']}" if run.get("url") else ""
    print(f"Result: {report['outcome']} (exit code {report['exit_code']}).{where}", file=sys.stderr)


def cmd_run_suite(args: argparse.Namespace) -> int:
    from .app_client import ApiError, AppUnavailableError, RequestOutcomeUnknownError
    from .gates import EXIT_ERROR, GateSpecError, parse_fail_if, parse_metrics, parse_tolerances
    from .money import validate_money_input

    report: dict[str, Any] = {"outcome": "error", "exit_code": EXIT_ERROR}
    try:
        try:
            budget = validate_money_input(args.budget)
        except ValueError as exc:
            raise RunSuiteError("--budget must be a decimal amount such as 0.50") from exc
        if budget <= 0:
            raise RunSuiteError("--budget must be greater than zero")
        if not 1 <= len(args.run_key) <= 200 or not args.run_key.strip():
            raise RunSuiteError("--run-key must have 1 to 200 characters")
        if args.timeout <= 0 or args.poll_interval <= 0:
            raise RunSuiteError("--timeout and the poll interval must be greater than zero")
        gates = parse_fail_if(args.fail_if)
        tolerances = parse_tolerances(args.tolerance)
        metrics = parse_metrics(args.baseline_metrics)
        if not args.baseline_run and (args.baseline_candidate or args.baseline_metrics or args.tolerance):
            raise RunSuiteError("--baseline-candidate, --baseline-metrics and --tolerance need --baseline-run")
        config = AppConfig.from_env(port=args.port)
        with _app_client(config) as client:
            _run_suite(
                client, config, args, budget=budget, gates=gates, metrics=metrics, tolerances=tolerances, report=report
            )
    except (RunSuiteError, GateSpecError, AppUnavailableError, ConfigError, DataDirError) as exc:
        report["error"] = str(exc)
    except RequestOutcomeUnknownError as exc:
        report["error"] = (
            f"{exc}. Whether the app received the request is unknown. Run the same command again (same --run-key): "
            "a run it already created is returned instead of a new one."
        )
    except ApiError as exc:
        report["error"] = f"The app answered: {exc}"
    except KeyboardInterrupt:
        report["error"] = "Stopped waiting. A started run continues in the app; the same --run-key follows it again."
    if "error" in report:
        print(f"error: {report['error']}", file=sys.stderr)
    else:
        _print_summary(report)
    print(json.dumps(report, indent=2))
    return int(report["exit_code"])


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="foundry-eval", description="Foundry Model Evaluator")
    parser.add_argument("--version", action="version", version=__version__)
    sub = parser.add_subparsers(dest="command")

    serve = sub.add_parser("serve", help="start the local web app (default)")
    serve.add_argument("--port", type=int, default=None)
    serve.add_argument("--no-browser", action="store_true")
    serve.add_argument("--dev", action="store_true", help="also accept the Vite dev server origin")
    serve.set_defaults(func=cmd_serve)

    export = sub.add_parser("export-openapi", help="write the OpenAPI schema (for frontend type generation)")
    export.add_argument("--output", default="openapi.json")
    export.set_defaults(func=cmd_export_openapi)

    check = sub.add_parser("pricing-check", help="fetch live Azure prices and show how profiles match (free)")
    check.add_argument("--profile", action="append", default=[])
    check.set_defaults(func=cmd_pricing_check)

    smoke = sub.add_parser("smoke", help="small paid live test with an explicit budget")
    smoke.add_argument("--profile", action="append", required=True)
    smoke.add_argument("--budget", required=True, help="maximum estimated spend, e.g. 0.05")
    smoke.add_argument("--prompt", default="Reply with the single word: ready.")
    smoke.add_argument("--max-output-tokens", type=int, default=64)
    smoke.add_argument("--judge", help="also judge the answers with this profile (within the same budget)")
    smoke.add_argument("--judge-max-output-tokens", type=int, default=1024)
    smoke.set_defaults(func=cmd_smoke)

    suite_run = sub.add_parser(
        "run-suite", help="run a saved suite in the running app, wait for it and check gates (paid; for CI)"
    )
    suite_run.add_argument("--suite", required=True, help="the suite's id or exact name")
    suite_run.add_argument("--budget", required=True, help="maximum estimated spend for the run, e.g. 0.50")
    suite_run.add_argument(
        "--run-key", required=True, help="names this run, e.g. the CI build id; the same key never pays twice"
    )
    suite_run.add_argument(
        "--candidate",
        action="append",
        default=[],
        help="run only this suite candidate (id, label or profile name); repeatable",
    )
    suite_run.add_argument("--title", help="the run's title (default: the suite name and the run key)")
    suite_run.add_argument(
        "--fail-if",
        action="append",
        default=[],
        metavar="GATE",
        help="cost-upper-above=<amount>, failures-above=<n> (default 0) or incomplete-cost; repeatable",
    )
    suite_run.add_argument("--baseline-run", help="compare with this earlier run and fail on a regression")
    suite_run.add_argument(
        "--baseline-candidate", help="the baseline run's candidate (key or label), when it has several"
    )
    suite_run.add_argument("--baseline-metrics", help="comma-separated: judge, checks, cost (default: all three)")
    suite_run.add_argument(
        "--tolerance",
        action="append",
        default=[],
        metavar="METRIC=VALUE",
        help="allowed regression: judge=<score points>, checks=<share 0-1>, cost=<amount per answer>",
    )
    suite_run.add_argument("--port", type=int, default=None, help="the app's port (default: FME_PORT or 8765)")
    suite_run.add_argument("--timeout", type=float, default=3600.0, help="seconds to wait for the run (default 3600)")
    suite_run.add_argument("--poll-interval", type=float, default=2.0, help=argparse.SUPPRESS)
    suite_run.set_defaults(func=cmd_run_suite)

    args = parser.parse_args(argv)
    if args.command is None:
        args = parser.parse_args(["serve", *(argv or [])])
    try:
        return int(args.func(args))
    except (ConfigError, DataDirError, InstanceLockError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
