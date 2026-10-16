# Foundry Model Evaluator

A personal web app that runs on your own Windows machine. It compares Microsoft Foundry models side by side on answer
quality, token usage, latency and **estimated token cost**.

- **Compare.** Send one prompt to several models, or to one model with different settings (for example low vs high
  reasoning effort), and see the answers next to each other.
- **Suites and history.** Save prompts, models and settings as a suite and run it with repeated trials. You get average
  cost, latency, scores and failure counts over matching prompts and trials, and the coverage is always shown.
- **Charts.** Runs and suite comparisons are charted: quality against estimated cost per task (log scale, with the
  Pareto frontier and the most attractive quadrant), output tokens split into answer and reasoning, and latency. Every
  chart has a table view.
- **Scoring.** Rate answers yourself on a 1–5 scale with notes, or have a Foundry model judge them anonymously against
  a versioned rubric. Manual and judge scores are kept separate.
- **Models and pricing.** Manage endpoints and API keys, model profiles and rates. Rates come from the Azure Retail
  Prices API (applied only when the match is unambiguous), from manual entries with a source URL, or from labelled
  custom overrides. Projections estimate the cost per request, per 1,000 requests and per month.

Each response shows the total input, uncached input, cache reads and writes, output and reasoning tokens, the latency
and the estimated token cost. A value the provider did not report shows as **not reported**. An unknown count or rate
makes the estimate **incomplete**: it is shown as a lower bound, never as zero.

| Side-by-side comparison | Anonymized judge and manual scores |
|---|---|
| ![A run comparing three candidates with usage, latency and cost](docs/screenshots/run.png) | ![Judge evaluation with revealed labels and criterion scores](docs/screenshots/scoring.png) |
| **Suite comparison with coverage** | **Cost projection with what-if assumptions** |
| ![Suite runs compared on matching prompts and trials](docs/screenshots/suite.png) | ![Projected per-request, per-1,000 and monthly costs](docs/screenshots/projections.png) |
| **Quality vs. estimated token cost, with the Pareto frontier** | **Output tokens per task: answer and reasoning** |
| ![Judge score against average cost per task on a log scale, with the most attractive quadrant](docs/screenshots/chart-quality.png) | ![Stacked bars of answer and reasoning tokens per candidate](docs/screenshots/chart-tokens.png) |

## Quick start (Windows)

You need:

- [uv](https://docs.astral.sh/uv/getting-started/installation/) to install Python and the backend:
  `winget install --id=astral-sh.uv -e`
- [Node.js LTS](https://nodejs.org/) to build the web interface: `winget install --id=OpenJS.NodeJS.LTS -e`

In the repository folder, run:

```bat
scripts\setup.cmd
scripts\start.cmd
```

`start.cmd` opens <http://127.0.0.1:8765> in your browser. Press Ctrl+C in the window to stop the app. After pulling
new code, run `scripts\setup.cmd` again.

The `.cmd` files run the PowerShell scripts without changing your execution policy. `setup.cmd -Dev` also installs the
test tools, and `start.cmd -Port 8766 -NoBrowser` changes the port and skips opening the browser.

### First steps

1. In **Models & pricing → Endpoints & keys**, add your Foundry resource URL (for example
   `https://my-resource.services.ai.azure.com`) and its Azure region. Paste the API key and choose **Save key**. Use
   **Verify (free)** to check the key without making a paid call.
2. In **Model profiles**, add a profile for each deployment. Choose the API (OpenAI Responses, Chat Completions or
   Claude Messages) and a capability preset. Then enter the deployment name, and the meter terms used in Azure price
   names (for example `5.6 terra`).
3. In **Rates**, choose **Refresh Azure prices**, then **Match Azure prices**. Claude models are billed through
   Microsoft Marketplace and are not in the Retail Prices API, so enter their rates manually, with the source URL.
4. On **Compare**, write a prompt, add candidates, then choose **Run comparison**. A paid call is made only when you
   choose Run, and nothing is retried automatically.

## Where things are stored

| What | Where |
|---|---|
| API keys | Windows Credential Manager (service `FoundryModelEvaluator`). Never in files, the database, logs or the browser. |
| Data: database, logs, instance lock | `%LOCALAPPDATA%\FoundryModelEvaluator` |
| Python, virtual environment, built UI | `%LOCALAPPDATA%\FoundryModelEvaluator\runtime` (safe to delete; `setup.cmd` recreates it) |

**OneDrive.** The repository may live in a OneDrive-synced folder, but the app's data must not. The app refuses to
start if the data folder is inside the repository or under OneDrive. To keep `node_modules` and the virtual
environment out of OneDrive, `setup.cmd` builds the web interface from a copy under `runtime`. To use another data
folder, set `FME_DATA_DIR` to a local path outside OneDrive.

## Command line

Close the app first; the commands use the same data folder.

```bat
scripts\foundry-eval.cmd pricing-check --profile "My profile"
scripts\foundry-eval.cmd smoke --profile "Model A" --profile "Model B" --budget 0.05 --judge "Judge model"
```

- `pricing-check` is free. It fetches live Azure prices and shows how each profile's meters match, with the reasons.
- `smoke` makes a few tiny paid calls within an explicit budget, optionally followed by a judge evaluation, and prints
  usage and costs. Its results appear in History.

See [docs/smoke-test.md](docs/smoke-test.md) for a step-by-step live check.

## Export

On a run's page, **Download JSON** and **Download CSV** save the run as a file. The export only reads stored data and
the price snapshots frozen with the run, so it never makes a call to a model.

- **JSON** (`fme.run.v1`) holds the run, prompts, candidates (settings, profile and the rates with their meter,
  source and dates), every attempt (retries included, with usage, latency and cost), manual scores, judge
  evaluations with criterion scores, the run totals, and notes on how to read the costs.
- **CSV** has one row per attempt, with the usage, latency, cost, manual score and latest judge score in separate
  columns.
- **Costs** are exact decimal strings. An empty cell or `null` means *not reported*, and `cost_status` says why
  (`complete`, `incomplete` with a lower bound, `not_sent`, or `pending`). An unknown cost is never written as 0.
- **Left out by default:** prompt text, answers, reasoning summaries, manual-score notes and judge explanations.
  Tick **Include prompt and answer text** to add them. Never included: API keys, the endpoint URL, request bodies and
  raw provider responses.
- **Spreadsheets.** In the CSV, text that starts with `=`, `+`, `-`, `@`, a tab or a carriage return gets a leading
  `'`, so a model's answer or a title cannot run as a formula when you open the file in Excel.

The same files come from `GET /api/runs/{run_id}/export?format=json|csv&include_text=false`.

## Limits of version 1

- Windows, USD prices, text-only single-turn prompts with optional system instructions, and pay-as-you-go token
  pricing.
- Not included: provisioned throughput (PTU), batch pricing, images and audio, tool calls, importing responses from
  elsewhere, team hosting, and reconciliation with invoices.
- Costs are **estimates** from token counts and published or entered rates. They are not bills.
- Model capabilities change, so the capability presets are starting points; edit a profile's capabilities when a
  model differs.

## Documentation

- [Token accounting](docs/token-accounting.md): how usage is normalized and priced, with worked examples.
- [Pricing](docs/pricing.md): Azure price matching, manual rates, overrides, snapshots and projections.
- [Security](docs/security.md): the threat model, key handling, the local-only server and paid-call safety.
- [Live smoke test](docs/smoke-test.md): checking real endpoints and prices with a small budget.
- [Implementation plan](docs/implementation-plan.md)

## Development

```bash
cd backend && uv sync && uv run pytest -q && uv run ruff check . && uv run mypy src
cd frontend && npm ci && npm run typecheck && npx vitest run && npm run build && npx playwright test
```

The browser tests start the app on a temporary data folder against a mock Foundry server
(`python -m foundry_eval.devtools.mock_foundry`), so they make no paid calls. For UI work, run
`uv run foundry-eval serve --dev` together with `npm run dev`.
