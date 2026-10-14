# Foundry Model Evaluator — implementation plan

## Context
The user wants a personal, local web app that compares Microsoft Foundry models on response quality, token usage,
latency and **estimated token cost**: run prompts and saved suites across models and settings, score the answers
manually or with a Foundry judge, and project costs. It's a new project (empty working directory) and is built from
the user's full spec. Research done this session pinned down the current API facts behind the design:
- OpenAI and Claude usage fields, from the SDK sources.
- The `AsyncAnthropicFoundry` client.
- GPT-5.6 `cache_write_tokens`.
- Real Azure Retail Prices meter-name styles.

**Delivery** (the user's choices):
- **Repo.** The new private repo is `koala-man-64/foundry-model-evaluator`. This session can't create repos, so the
  user creates it empty. Step 0 attaches it with `add_repo` (push access) to confirm early that it can be reached.
- **Branches.** A README-only commit goes on `main`. The full app goes on a feature branch as **one draft PR**, with
  separate commits for increment 1, increment 2, and review fixes.
- **Out of reach from here.** This container can't reach `prices.azure.com`, learn.microsoft.com or Azure endpoints.
  The price matcher is therefore tested against fixtures modeled on real meter names. The **live smoke test** and the
  **live pricing check** are CLI commands that the user runs on Windows, with their own keys and an explicit budget.

## Stack and architecture
| Area | Decision |
|---|---|
| Backend | Python ≥3.12, FastAPI, uvicorn (1 worker, no reload), pydantic v2, stdlib `sqlite3` with **STRICT** tables, UUID text ids, UTC ISO timestamps, and SQL migrations tracked by `PRAGMA user_version`. Plus `decimal`, `keyring`, `truststore` (for corporate TLS inspection) and `uv` with a lockfile. Package `backend/src/foundry_eval`; CLI `foundry-eval serve|pricing-check|smoke|export-openapi`. |
| Interfaces | Four `Protocol`s, each with a registry. The engine and API depend only on these. They are `ProviderAdapter.execute → ProviderResult`, `UsageNormalizer.normalize → UsageFacts`, `PricingResolver.resolve → RateCard` (with explanations) and `Judge.evaluate → EvaluationResult`. |
| OpenAI-compatible adapters | Raw `httpx2` (the maintained httpx successor, already used by both official SDKs) behind one `http.py` shim. This gives full control of the auth header (`api-key` or Bearer), the URL and `api-version`, the raw JSON, SSE parsing, explicit connect/read/total timeouts, and **no retries**. There are two adapters: Responses (always sends `store:false` and `truncation:"disabled"`) and Chat Completions (streams only when the profile sets `include_usage`; otherwise non-streaming). |
| Claude adapter | Official `anthropic` SDK `AsyncAnthropicFoundry`, as the bundled Claude API guidance recommends. Settings: `max_retries=0`, an explicit timeout, and the truststore TLS context through `DefaultAsyncHttpxClient`. It calls `messages.stream()` then `get_final_message()`, and SDK debug logging is off. `thinking` and `output_config.effort` are typed parameters; SDK 1.x dropped `temperature`, `top_p` and `top_k`, so older models get them through `extra_body`. The stored request body is the exact arguments the app passed. There's **no refusal fallback**, so every result comes from the selected model. |
| Frontend | React 19, TypeScript 5.9 (not 7, which `openapi-typescript` can't use yet), Vite 7, react-router 7, TanStack Query 5. Built from native HTML elements and a small CSS design system, with no UI library, so a strict CSP works. API types are generated from the backend's OpenAPI document. Responses render as plain text, with an optional Markdown view (`react-markdown`, no raw HTML, no remote images). |
| Tests | pytest with `hypothesis`, Vitest with Testing Library, and Playwright 1.56.1 (matches the preinstalled Chromium). A **mock Foundry server** imitates OpenAI Responses and Chat (JSON and SSE), Anthropic Messages (SSE) and paged Retail Prices; magic deployment names trigger 500s, 429s, delays and malformed judge output. |
| Windows | `scripts/setup.ps1` and `start.ps1`, each with a `.cmd` wrapper to get past PowerShell execution policy. They use **uv-managed CPython**, because Microsoft Store Python redirects writes to `%LOCALAPPDATA%`. `UV_PROJECT_ENVIRONMENT` keeps the virtualenv out of the OneDrive-synced repo. Startup opens `http://127.0.0.1:8765`. |
| CI | `.github/workflows/ci.yml`, using the Actions v7 majors (verified as current). Ubuntu runs the full suite (backend, frontend, e2e); Windows runs the backend tests. |
| Scope | v1 covers Windows, USD, text-only single-turn prompts with an optional system prompt, and pay-as-you-go token pricing. Deferred: provisioned capacity, multimodal input, tools, imported responses, team hosting, invoice reconciliation, and automatic model discovery (metadata is entered by hand). |

## Endpoints, profiles, settings
- An **endpoint** has:
  - a resource base URL, which must be HTTPS (plain HTTP is allowed only for loopback, for the mock server and local
    proxies)
  - a region, used for pricing
  - an auth style, `api-key` by default or Bearer
  - a random `credential_ref`

  If the endpoint's host changes, its stored key is deleted. An optional **verify** action makes a free
  `GET /openai/v1/models` call.
- A **profile** is a deployment on an endpoint. It records:
  - the API style (`openai_responses`, `openai_chat` or `anthropic_messages`)
  - a route preset:
    - `openai_v1`: `/openai/v1/...`, with no api-version; the default for every OpenAI-compatible model
    - `foundry_models`: `/models/chat/completions?api-version=…`, editable, default `2024-05-01-preview`
    - `anthropic`, or a custom path
  - the deployment, sent as `model`, and the metadata: publisher, model name, version, context window, max output
  - the deployment type (Global, Data Zone or Regional) and the processing tier (Standard or Priority)
  - the capability descriptor and pricing configuration
  - the streaming and `include_usage` flags and the timeouts
- **Settings** are provider-neutral: `max_output_tokens`, `temperature`, `top_p`, `top_k`, `stop`, `seed`, `effort`,
  `thinking_mode`, `thinking_budget_tokens`, `thinking_display`, `reasoning_summary`, `verbosity`, and
  `prompt_cache_ttl` (Claude: none, 5m or 1h on the last user block).
- The **capability descriptor** says whether each setting is supported, gives its allowed values or range and the
  provider's default, and maps it to a parameter name (`max_completion_tokens` or `max_tokens`; `system` or
  `developer` role). It also records whether structured output is available.
  - Editable presets: OpenAI reasoning, OpenAI standard, Claude adaptive (4.6 and later), Claude legacy budget
    thinking, and generic Chat.
  - The UI **disables** unsupported controls and says why. A value left behind after switching profiles is flagged and
    blocks the run.
  - The backend returns 422, listing the fields, for unsupported or out-of-range settings. It re-checks saved suite
    candidates when a run is created. Nothing is ever silently dropped.
- **Candidates.** Each candidate gets a `candidate_key`, a hash of the profile's model and pricing identity plus its
  canonical settings. The same profile can appear more than once with different settings, for example different
  effort levels. Labels show only the settings that differ between candidates.
- **What each attempt stores:** the exact prompt text, the requested settings, the exact provider request body, the
  `model` and `service_tier` the provider echoed back, and whether the response was streamed. Omitted settings show as
  "provider default".

## Token accounting
Each normalizer returns **UsageFacts**: only what the provider reported, plus the untouched raw usage. A missing value
is `None`, shown as "not reported" and never 0.

| Fact | Responses | Chat Completions | Claude Messages |
|---|---|---|---|
| Total input | `input_tokens` | `prompt_tokens` | sum of the three Claude input fields |
| Cache read | `input_tokens_details.cached_tokens` | `prompt_tokens_details.cached_tokens` | `cache_read_input_tokens` |
| Cache write | `input_tokens_details.cache_write_tokens` (GPT-5.6+; Azure may omit it) | `prompt_tokens_details.cache_write_tokens` | `cache_creation_input_tokens`; 5m/1h from `cache_creation` |
| Uncached input | total − reads − writes | total − reads − writes | `input_tokens` |
| Billed output | `output_tokens` (includes reasoning) | `completion_tokens` (includes reasoning) | `output_tokens` (includes thinking) |
| Reasoning (part of output, display only) | `output_tokens_details.reasoning_tokens` | `completion_tokens_details.reasoning_tokens` | `output_tokens_details.thinking_tokens` if present (approximate) |

**Calculator.** It prices disjoint categories: uncached input, cache read, cache write 5m, cache write 1h, cache write
(no TTL given), and output. It computes Σ tokens × price ÷ unit size in `Decimal`; money is stored as TEXT, sent in
JSON as strings, and rounded only for display.
1. Each rate category is `priced`, `not_applicable` or `unknown`. `not_applicable` must come from a preset or the
   user, with its source recorded; it's never inferred from "no meter found". An unreported cache read or write folds
   into uncached input only when its category is `not_applicable`.
2. **Complete** means three things hold:
   - every count is known, and is either zero or priced
   - there are no anomalies, unrecognized non-zero `*_tokens` fields, non-zero audio or image tokens, or mismatches
     between the served and priced tier
   - the context tier is resolved
3. Otherwise the estimate is **incomplete**: it states its reasons, a lower bound (known costs, with unassigned tokens
   at the cheapest possible rate) and, when every rate involved is known, an upper bound. It displays as
   "≥ $x — incomplete: cache writes not reported" and never as a plain total.
4. **Anomalies:** reads + writes > total, Claude's 5m + 1h ≠ its cache-write total, and negative derived values. Claude
   writes with no TTL split use the request's single TTL and are labeled "inferred".
5. Known subset fields such as `text_tokens` and prediction tokens are shown but never billed. Reasoning tokens are
   **never billed twice**.
6. **Context tiers** (for example LongCo above 272K; some models use a lower threshold) are chosen per request from its total input. If the
   total input is unknown, the estimate is incomplete.
7. **Attempts without usage.** `request_sent_at` is written before each HTTP call. An attempt that was never sent
   (cancelled while queued, `skipped_budget`, interrupted before sending) costs exactly **$0** and shows "not sent". An
   attempt that was sent but returned no usage (cancelled, failed or interrupted mid-call) is **incomplete, never $0**.
8. Totals are reported per run and per suite: generation, judging, and the combined total, each with its own status.
   Averages and projections use complete attempts only and show how many were excluded.

## Pricing
- **Rate card.** Rates are stored per profile, per category and per tier (`tier_min_input`). Each rate's source is one
  of:
  - `azure_auto`
  - `azure_pinned`, a meterId read from the **cached catalog**; the network is used only on an explicit refresh
  - `manual`, with a source URL (required) and an effective date
  - `not_applicable`

  A **custom override** can sit on top. It needs a note and is always badged "custom rate".
- **Retail client.** It queries `https://prices.azure.com/api/retail/prices?api-version=2023-01-01-preview` with
  `currencyCode='USD'`.
  - Filters: `armRegionName eq '<region>' and priceType eq 'Consumption'`, combined with configurable
    product/service clauses. The defaults are `serviceName eq 'Foundry Models'` and
    `contains(productName,'Azure OpenAI')`; the legacy `Cognitive Services` filter is optional.
  - It follows `NextPageLink`, parses prices with `parse_float=Decimal`, and de-duplicates by meterId, taking the
    latest `effectiveStartDate` that isn't in the future.
  - Results are cached in SQLite along with the retrieval time.
- **Units.** `unitOfMeasure` is authoritative: `1K`, `1M`, `1K Tokens` and `1M Tokens` are accepted. Hourly and
  unknown units are rejected. If a unit in the meter name disagrees with `unitOfMeasure`, the match is ambiguous.
- **Residual matcher.** It normalizes each meter name and strips the recognized attribute tokens:
  - direction: `Inp`, `Outp`, `Opt`
  - cache: `cached Inp`, `Cd Wr`
  - deployment type: `glbl`/`Gl`, `DZ`, `rgnl`
  - processing tier: `Std`, `PP` (no token means Standard)
  - context tier: `ShortCo`, `LongCo` (no token means a single tier)
  - modality: `Text`
  - unit words

  What remains must equal the profile's editable `meter_terms`, ignoring case, spaces and hyphens. For example,
  `5.6 terra` matches without matching `5.6 luna`, and `4o` doesn't match `4o mini`. Some meters are never matched:
  any with an unrecognized or excluded token (Batch, PTU, FT, Audio, Image) or a missing required attribute. Exactly
  one meter per category resolves automatically. Otherwise the category shows "ambiguous" or "not found", with ranked
  candidates and reasons, so the user can pin a meter or enter a manual rate. Missing a match is acceptable; a wrong
  match is not.
  - A ShortCo/LongCo pair becomes one tiered card, with its threshold on the profile.
  - Several `tierMinimumUnits` rows count as ambiguous.
  - Claude isn't in the Retail API, so its rates are manual, with the Anthropic and Azure Marketplace pricing pages
    suggested as sources.
- **Snapshots.** When a run is created, each distinct profile's card is frozen into
  `price_snapshots`/`price_snapshot_rates`. Each rate keeps its value, original unit, per-1M value, source, URL or
  meter IDs, `effectiveStartDate`, `retrieved_at` and currency. Triggers abort UPDATE and DELETE on snapshot tables.
  Judge evaluations take their own snapshot. A refresh changes only future snapshots and "current price" projections,
  which warn when a profile's pricing identity has changed since the run.

## Execution
- **Concurrency.** One global `asyncio.Semaphore` (default 3, configurable 1–10) covers generation and judge calls.
  Each attempt runs as its own task that catches everything and records its own outcome (no `TaskGroup`), so
  successes survive another model's failure.
- **Latency** is timed from after the attempt gets a concurrency slot to the end of the response. Time to first token
  is recorded when streaming. Comparisons that mix streamed and non-streamed results are flagged.
- **No automatic retries.** `POST /attempts/{id}/retry` and "retry failed" create new attempts with `retry_of`. Every
  paid action (runs, evaluations, retries) requires an `Idempotency-Key`, so a double click doesn't pay twice.
- **Cancel.** Queued attempts become "not sent". In-flight attempts have their task cancelled and the connection
  closed, and are marked cancelled with "provider may still bill; usage not reported".
- **Restart.** An exclusive OS **instance lock** in the data directory is taken *before* recovery. Recovery then marks
  queued or running attempts and evaluations, and their runs, as `interrupted`. They are never replayed. A second
  instance is refused before it can touch anything.
- **Budget (optional).** `POST /runs/preview` shows the validation results, pricing completeness, the number of calls,
  and the worst-case cost. When a budget is set:
  - The run is refused if any rate is unknown or `max_output_tokens` isn't set.
  - Before each call (judge calls included), the worst case is reserved: UTF-8 bytes plus framing, times the higher of
    the input and cache-write rates, plus `max_output_tokens` times the output rate. The reservation settles to the
    actual cost afterwards.
  - A call that would exceed the budget becomes `skipped_budget` and costs $0.
- **Progress.** The UI polls `GET /runs/{id}/progress` about once a second while a run is active, with no SSE broker.
  The adapters still stream from the provider.
- **Finish reason.** A normalized `finish_reason` (completed, max_tokens, content_filter, refusal, context_exceeded,
  error) is kept separate from the lifecycle status, and the raw value is stored too.

## Scoring and judge
- **Manual scores.** A 1–5 overall rating with notes, one per attempt, stored separately from judge scores. This lands
  in increment 1, since it costs nothing.
- **Rubrics** are versioned, and a version is immutable once used. The editable default has four criteria
  (instruction following, correctness, completeness, clarity), scored 1–5 with equal weights. Validation requires at
  least one criterion, unique keys, weights above 0 (normalized), and an integer scale with min below max.
- **Trigger.** The judge runs only on an explicit "Run judge" action, or when a suite's auto-judge setting is frozen
  into the run at creation. There is one evaluation per (run, prompt, trial), over that group's succeeded attempts.
- **What the judge sees.** The system instructions and user prompt, the rubric, and the response texts labeled A, B,
  … in `secrets.SystemRandom` order, with the order and mapping stored. Responses sit inside random-nonce delimiters
  and are treated as data. The judge never sees model, profile or deployment names, costs, latency or reasoning
  summaries. Structured JSON output is requested where supported: Responses `text.format`, Chat `response_format`,
  Claude `output_config.format`.
- **Validation.** Labels must match exactly, criterion keys come from the rubric version, and scores are integers
  within the scale. The judge's overall score must be within the scale, and the preferred response must be a label or
  "tie". The judge's overall and an app-computed **weighted overall** are both stored. Invalid JSON, an unknown or
  missing label, or a stop at `max_tokens` gives status **malformed**. The raw output and errors are kept, attempts
  are untouched, and re-judging creates a new evaluation.
- **Oversized input** is rejected before any HTTP call, with status `rejected_oversized` and **no truncation**. The
  check uses a heuristic estimate shown in the UI against the judge's context window minus max output. A provider's
  context-length error also maps to `rejected_oversized`.
- **What each evaluation stores:** the judge profile snapshot, settings, rubric version, raw response, normalized
  scores, usage, price snapshot and cost. It also stores `judge_is_participant`, set when the judge shares a profile,
  an endpoint and deployment, or a model name with any candidate. Judging cost is shown separately from generation
  cost.

## Suites, comparisons, projections
- A **suite** has prompts, candidates, a trial count, and an optional auto-judge. A suite run covers prompts ×
  candidates × trials.
- **Comparisons.** A cell is (prompt content hash, trial index); runs of the same suite can be compared. Metrics use
  only the cells where every selected candidate has a usable attempt, and coverage is shown as k of n cells. Failures
  are counted across all cells. Each candidate shows average cost and latency, average manual and judge scores (kept
  separate), failure counts, and win rate.
- **Projections.**
  - Each attempt is re-priced under the scenario, with its tier re-checked after the what-if multipliers, and the
    resulting costs are averaged. Prices are never applied to averaged token counts.
  - Output: cost per request, per 1,000 requests, and per month for a user-entered volume, per candidate.
  - What-ifs: input and output length multipliers, and the cache read and write shares. These are labeled
    **assumptions**, and a control is disabled when its category is not applicable.
  - The price basis can be the run's original snapshot or current prices.

## Security and data
- **Network binding.** The server binds to 127.0.0.1 only, on port 8765; the CLI rejects any other host.
- **Every request:** the Host must be `127.0.0.1:<port>` or `localhost:<port>`, which blocks DNS rebinding.
  `Sec-Fetch-Site`, when present, must be `same-origin`, or `none` for a top-level GET. This rejects `same-site`
  requests, which include other localhost ports.
- **State-changing requests** also need an `Origin` equal to the app origin, JSON content type, and
  `X-FME-Client: 1`.
- **Headers.** There is no CORS. Built assets get a strict CSP (`default-src 'self'`, `frame-ancestors 'none'`),
  `nosniff` and `no-referrer`. The Vite dev origin is allowed only with an explicit dev flag.
- **Keys.**
  - Keys are stored with `keyring`, using an explicitly selected `WinVaultKeyring`, under service
    `FoundryModelEvaluator` and user `credential:<credential_ref>`.
  - The key API is write-only (`PUT/DELETE /endpoints/{id}/key`) and returns only `key_configured`.
  - The app refuses fail and plaintext backends. An in-memory backend exists only with `FME_KEYRING=memory`, for
    tests.
  - Keys are read just in time and never logged: a redaction filter covers registered secrets and auth headers, and
    the httpx2, httpcore and anthropic loggers run at WARNING.
  - Error text is sanitized before it's saved.
  - The browser's key input is cleared once submitted and is never put in browser storage.
  - The package that holds this code is named `credentials`, so it doesn't shadow Python's stdlib `secrets` module.
- **Data.** Data lives in `%LOCALAPPDATA%\FoundryModelEvaluator` (database, logs, instance lock), with an
  `FME_DATA_DIR` override. Startup refuses a data directory inside the repo or under an OneDrive environment path.
  `docs/security.md` states the threat model: other processes running as the same user are trusted.

## Data model (STRICT, all money TEXT)
- **Configuration:**
  - `app_settings`
  - `endpoints` (base_url, region, auth_style, credential_ref, key_set_at)
  - `model_profiles` (endpoint_id, api_style, route, api_version, deployment, metadata, deployment_type,
    processing_tier, capabilities_json, streaming, include_usage, timeouts, meter_terms, tier_threshold, archived)
- **Pricing:**
  - `profile_rates` (category, tier_min_input, source, meter_id, price, unit_label, unit_tokens, source_url,
    effective_date, is_override, note)
  - `price_catalog` (+ fetch log)
  - `price_snapshots` and `price_snapshot_rates` (immutable)
- **Runs:**
  - `runs` (kind, suite_id, idempotency_key UNIQUE, status, budget, judge_config_json)
  - `run_prompts` (system/user text, content_hash)
  - `run_candidates` (profile_snapshot_json, settings_json, candidate_key, price_snapshot_id)
  - `attempts`, request side: trial_index, retry_of, status, request_body_json, request_sent_at, latency_ms, ttft_ms,
    streamed
  - `attempts`, response side: finish_reason and raw value, response_model, service_tier, output_text,
    reasoning_summary, raw_response_json, usage columns, anomalies_json, cost_json, cost_status, cost_lower,
    cost_upper, error_text
- **Scoring:**
  - `manual_scores`
- **Increment 2:**
  - `suites`, `suite_prompts`, `suite_candidates`
  - `rubrics` and `rubric_versions`
  - `evaluations` (trigger, status, judge snapshot and settings, rubric_version_id, price_snapshot_id, order_json,
    request, raw response, parse_errors, participant flag, preferred or tie, usage, cost)
  - `evaluation_candidates` (label, position, judge_overall, weighted_overall, explanation)
  - `evaluation_criteria`

## Local API (`/api`, money serialized as strings, response models everywhere)
- **Configuration:**
  - `settings`
  - `endpoints` CRUD, `endpoints/{id}/key`, `endpoints/{id}/verify`
  - `profiles` CRUD, `capability-presets`
- **Pricing:** `pricing/refresh`, `pricing/catalog?region&q`, `profiles/{id}/rates` (GET/PUT), and
  `profiles/{id}/rates/match`.
- **Runs:**
  - `runs/preview`; `runs` (POST and list)
  - `runs/{id}`, `runs/{id}/progress`, `runs/{id}/cancel`
  - `attempts/{id}/retry`, `attempts/{id}/raw`, `attempts/{id}/manual-score`
- **Increment 2:**
  - `suites` CRUD and `suites/{id}/runs`
  - `rubrics` and `rubrics/{id}/versions`
  - `runs/{id}/evaluations`, `evaluations/{id}`
  - `comparisons`, `projections`
- `health`

## UI (four areas)
- **Compare:** prompt editors (system and user, "load from suite"), candidates with capability-driven settings and
  duplicate-to-vary, then preview and Run. The live side-by-side cards show:
  - status and the answer
  - a usage table ("not reported" where missing)
  - latency, time to first token, and finish reason
  - estimated token cost with breakdown, bounds and reasons
  - links to the exact request and the raw response, plus retry and a manual score
- **Suites & history:** suite editor, run history and detail, and comparisons with coverage.
- **Scoring:** manual ratings; the judge panel (profile, settings, rubric, scope, size check, participant warning);
  evaluations, with criterion explanations, the revealed order, and raw output; the rubric editor.
- **Models & pricing:**
  - endpoints, with a write-only key field and verify
  - profiles
  - rates: match candidates, pin, manual entry with source URL, custom overrides, refresh
  - projections
  - settings

Every amount is labeled **"Estimated token cost"**. Custom rates are badged and incomplete estimates are flagged.
Generation, judging and combined totals are always separate.

## Build order (each step lands with its tests)
**Step 0.** Attach the repo with `add_repo`. If that fails, tell the user how to fix access and keep working locally in
`/home/user/foundry-model-evaluator`.

**Increment 1, about 60% of the work:**
1. **Scaffold.** Pinned dependencies, ruff, mypy, pytest, `uv.lock`, the Vite app, CI, scripts, `.gitignore` and
   `.gitattributes`.
2. **Foundations.** Paths, instance lock, database and migration 0001 with triggers, credentials, logging and
   redaction, the HTTP guard and headers.
   - **Gate:** guard test matrix, path and backend refusals, second-instance test, migration and trigger tests.
3. **Accounting core.** Normalizers, units, calculator, golden fixtures, `hypothesis` invariants.
   - **Gate:** 100% branch coverage on `usage/`, `pricing/calculator.py` and `pricing/units.py`.
   - **Independent accounting review** happens here, by a fresh subagent that recomputes at least 10 fixtures by hand.
4. **Pricing.** Retail client, catalog, residual matcher, resolver, snapshots.
   - **Gate:** positive and negative matcher fixtures with **zero false positives**; snapshot immutability; a refresh
     leaves history unchanged.
5. **Providers and mock Foundry server.**
   - **Gate:** contract tests across streaming and non-streaming, usage, finish reasons, errors and timeouts, with
     exactly one request per attempt.
6. **Execution and API.** Engine, preview, budget, idempotency, progress, recovery, routes, static serving, and the
   CLI.
   - **Gate:** at most 3 concurrent calls, failure isolation, cancel and interrupt cost rules, no replay, money
     serialized as strings.
7. **UI.** Models & pricing, Compare, History, and manual scoring.
   - **Gate:** Playwright specs for configuration, comparison (disabled controls, a failure, cancel, retry), pricing
     sources, history with price preservation, and the secret canary scan.

**Gate 1.** Everything green: pytest, ruff, mypy, `tsc`, Vitest, build, Playwright. Run the **independent
secrets/security review** (fresh subagent), fix its findings, then commit increment 1.

**Increment 2, about 40% of the work:**
8. **Suites.** Migration 0002, suites, suite runs, comparisons and coverage.
9. **Scoring.** Rubric versions and judging: anonymization, structured output, validation, oversized rejection,
   participant flag, cost snapshot, auto-judge.
10. **Projections.** Per-attempt pricing, tier crossing, what-ifs, and the price-basis switch.
11. **UI and end-to-end tests.** Suites, Scoring and Projections screens, with Playwright flows for each.

**Gate 2.** Everything green. Then commit increment 2.

**Hardening and delivery:**
12. **Review round two.** Fresh subagents review judge cost and projection accounting, then secret handling, plus the
    `security-review` skill on the branch. Fix findings with regression tests, and commit.
13. **Docs and screenshots.** README (Windows quick start, OneDrive notes, limits), `docs/token-accounting.md` (worked
    examples), `pricing.md`, `security.md` and `smoke-test.md`, plus Playwright screenshots for the PR.
14. **Push.** Push the README to `main` and the branch, open the draft PR, then watch CI and fix any failures.

## Test matrix
| Acceptance item | Coverage |
|---|---|
| Usage fixtures, cache accounting, reasoning | 3 API styles × streaming/non-streaming × details absent/zero/present × unknown fields; OpenAI reads and writes inside the total; Claude 3-way sum; TTL split, mismatch and inferred cases; reads + writes > total is an anomaly; reasoning billed once |
| Units, tiers, Decimal | 1K/1M units, conflicting unit in the name, hourly rejected; tier threshold at −1, equal and +1; unknown total input; volume tiers ambiguous; exact Decimal costs; property test that the categories sum to the total; money in JSON as strings; STRICT tables reject floats |
| Missing usage or prices | "not reported"; incomplete with bounds; never $0 for unknowns; $0 for "not sent" |
| Historical prices | Refreshing or editing rates after a run leaves its costs unchanged; triggers abort writes; the projection basis switches |
| Failures, cancellation, interruptions | One candidate gets a 500/429/timeout and the others succeed; error text sanitized; exactly one request; queued attempts become "not sent"; in-flight attempts become incomplete; restart marks work interrupted and the mock's request count stays the same; a second instance is refused; idempotent POSTs |
| Judge | No identities, costs or latency in the judge request; seeded order persisted; rubric validation; malformed cases (bad JSON, missing or out-of-range criteria, unknown label, max_tokens stop) keep raw output; oversized input makes zero HTTP calls; participant flag; judge cost separate and combined = sum |
| Security | Host/Origin/Sec-Fetch matrix; write-only key API; a canary key is absent from the DB, WAL, logs, API responses and browser storage; key deleted when the host changes; non-loopback bind rejected |
| Browser workflows | Playwright: configuration, comparison, history and retry, suites, scoring, projections, and the "Estimated token cost" labels |

## Verification
- **Backend:** `cd backend && uv run pytest -q --cov && uv run ruff check . && uv run mypy src`
- **Frontend:** `cd frontend && npm run typecheck && npx vitest run && npm run build && npx playwright test`.
  Playwright runs the real app on a temp data dir with `FME_KEYRING=memory` against the mock Foundry server.
- **Manual check:** run `foundry-eval serve` against the mock server and screenshot each area.
- **User, on Windows** (`docs/smoke-test.md`):
  - `foundry-eval pricing-check --profile …` is free and shows live Azure matches with their reasons.
  - `foundry-eval smoke --profile A --profile B --budget 0.05 [--judge J]` makes paid calls. It refuses to run
    without a budget, resolved rates and `max_output_tokens`. It uses tiny prompts, runs sequentially, reports spend
    against the budget, and its results appear in History.

## Risks
- **Meter names can't be checked live from here.** Mitigations: explainable candidates, pinning, manual rates with a
  source URL, and the `pricing-check` command.
- **Azure may not report GPT-5.6 cache writes.** Those estimates show as incomplete with bounds. The user can declare
  the category `not_applicable` (recorded and labeled) or set an override.
- **Capability presets drift as models change.** Descriptors are editable, and provider 4xx errors surface as failed
  attempts with the provider's message.
- **Token estimates for the budget and judge size check are heuristics.** The budget uses a conservative worst case,
  and nothing is ever truncated.
