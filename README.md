# Foundry Model Evaluator

A personal, local web application for comparing Microsoft Foundry models on **response quality, token usage,
latency, and estimated token cost**.

- Compare one prompt (or a saved prompt suite) across models and generation settings, side by side.
- Score answers manually or with a configured Foundry judge model.
- Normalize provider usage into disjoint billing categories and estimate cost from Azure Retail Prices or
  manually sourced rates, with immutable per-run price snapshots.
- Project costs for a monthly workload from observed usage.

The application runs only on your machine (loopback-only), stores API keys in the Windows Credential Manager, and keeps
its data under `%LOCALAPPDATA%\FoundryModelEvaluator`.

> Status: initial implementation in progress — see the open pull request.
