# Live smoke test

The automated tests use a mock Foundry server, so the first time you use real endpoints, check them with these steps.
Steps 1–3 are free. Step 4 makes a few tiny paid calls within a budget you choose.

## 1. Set up

Run `scripts\setup.cmd`, then `scripts\start.cmd`. In the app, add your endpoint, its key and your model profiles, as
described in the [README](../README.md#first-steps). Then:

- **Verify the key.** Choose **Verify (free)** on the endpoint. It lists models on the OpenAI v1 route and never makes
  an inference call. Some resources don't expose that list; a key that is rejected shows *HTTP 401*.
- **Output limit.** The smoke test caps each answer at 64 output tokens (`--max-output-tokens` changes it), so every
  call's cost is bounded, which a budget needs.
- **Enter Claude rates.** For Claude models, enter all five rates manually in **Rates**, each with its source URL.

Close the app before the next steps; the command line uses the same data folder.

## 2. Check live prices (free)

```bat
scripts\foundry-eval.cmd pricing-check --profile "GPT-5.6 terra"
```

For each billing category, the output shows the meter that matched, or why none did. Typical results:

- **matched** shows the meter name and its price. Compare it with the Azure pricing page.
- **ambiguous** means more than one meter fits. Pin the right one under **Rates → Pin meter**.
- **not_found** means the meter terms don't fit any meter. Fix them (they are the model words in Azure meter names,
  for example `5.6 terra`) or enter a manual rate.

The command also refreshes the cached catalog and applies automatic matches, like the **Refresh** and **Match**
buttons. Past runs keep their own price snapshots.

## 3. Check the prices in the app

Restart the app, open **Rates** and check that every category of each profile shows a price, or a deliberate *not
applicable*. Unknown rates make every estimate for that profile incomplete.

## 4. Make a small paid test

```bat
scripts\foundry-eval.cmd smoke --profile "GPT-5.6 terra" --profile "Claude Sonnet" --budget 0.05
scripts\foundry-eval.cmd smoke --profile "GPT-5.6 terra" --budget 0.05 --judge "GPT-4.1 judge"
```

The command refuses to run without a budget, complete rates and an output limit. It sends one short prompt
(`--prompt` changes it) to each profile, one call at a time. With `--judge`, it then has the judge score the answers,
and the judge call counts against the same budget.

It prints the worst case first, then for each answer:

- the status and finish reason
- the latency
- the usage: input, uncached, cache read, cache write, output and reasoning
- the estimated token cost, or the reasons it is incomplete

The judge result shows each label with its revealed profile and scores. Finally it prints the amount spent (an upper
bound) against the budget.

Check:

- **The usage looks plausible.** Reasoning should be inside the output and never added twice. For Claude, total input
  should be uncached + cache read + cache write.
- **The costs match a hand calculation** from the shown rates. See [token accounting](token-accounting.md).
- **The run appears in History** with the same numbers.

## Troubleshooting

| Symptom | Likely cause |
|---|---|
| `HTTP 404` on Chat Completions | Wrong route: try **Foundry Models** with an `api-version`, or check the deployment name. |
| `HTTP 400` naming a parameter | The model does not accept that setting; edit the profile's capabilities so the control is disabled. |
| *redirect* error | The resource URL is wrong or behind a gateway that redirects. Redirects are never followed, so your key is not sent elsewhere. |
| Every estimate *incomplete: cache writes not reported* | The deployment does not report `cache_write_tokens`. If it never bills cache writes separately, mark the category *not applicable*. |
| *No secure credential store* | Windows Credential Manager is unavailable for this account. |
| *The app is running* | Close the app before using the command line. |
