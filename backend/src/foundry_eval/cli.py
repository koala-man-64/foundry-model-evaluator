"""Command line entry point: ``foundry-eval serve | pricing-check | smoke | export-openapi``."""

from __future__ import annotations

import argparse
import asyncio
import json
import logging
import socket
import sys
import threading
import webbrowser
from decimal import Decimal, InvalidOperation
from pathlib import Path

from . import __version__
from .config import AppConfig, ConfigError
from .instance_lock import InstanceLock, InstanceLockError
from .logging_setup import configure_logging
from .paths import DataDirError, ensure_data_dir


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
        print("Note: the web interface is not built yet (run scripts\\setup.ps1 or npm run build in frontend).")
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
    from .execution.runs import CandidateSpec, PromptSpec, RunRequest, RunValidationError
    from .timeutil import new_id

    try:
        budget = Decimal(args.budget)
    except InvalidOperation:
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
        missing = [name for name in args.profile if name not in by_name]
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

        async def execute() -> None:
            services.engine.semaphore()  # bind to this loop
            services.engine.start(run_id)
            await services.engine.wait(run_id)

        services.settings.save(services.settings.get().model_copy(update={"concurrency": 1}))
        asyncio.run(execute())
        attempts = services.runs.attempts(run_id)
        spent = Decimal(0)
        failures = 0
        for attempt in attempts:
            candidate = services.runs.candidate(attempt.candidate_id)
            usage = attempt.usage
            cost = attempt.cost
            spent += cost.upper if cost and cost.upper is not None else (cost.lower if cost else Decimal(0))
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
        print(f"\nSpent (upper bound): {spent} of budget {budget}. Results are in the app's History (run {run_id}).")
        return 1 if failures else 0
    finally:
        lock.release()


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
    smoke.set_defaults(func=cmd_smoke)

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
