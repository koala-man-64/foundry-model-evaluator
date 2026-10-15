# Security

Foundry Model Evaluator is a single-user app that runs on your own computer. It makes paid calls with your API keys,
so its main jobs are to keep the keys secret, to accept requests only from its own page, and never to spend money
without an explicit action.

## Threat model

**What it protects against:**

- **Web pages in your browser.** Another site cannot read the app, change it, or start paid calls, even though the app
  runs on `localhost`.
- **Other machines on your network.** The server listens on the loopback address only.
- **Key leaks through the app.** Keys never reach the page, the database, logs, error messages, raw provider
  responses, or a host other than the endpoint they belong to.
- **Accidental spending.** Nothing is retried automatically, double clicks don't pay twice, an optional budget limits
  each run, and unfinished work is never replayed after a restart.
- **Synced folders.** Prompts, answers and history stay out of OneDrive and out of the repository.

**What it trusts:** other programs running as your Windows user. They can read your Credential Manager entries and the
app's data folder, and they can call the local API (there is no per-launch token). Use the app on a machine and
account you control. The provider's own billing and data handling are outside the app.

## Local-only server

- The server binds to `127.0.0.1` only. The command line refuses any other host.
- **Every request** needs a `Host` of `127.0.0.1:<port>` or `localhost:<port>`, which blocks DNS rebinding.
  `Sec-Fetch-Site`, when the browser sends it, must be `same-origin`, or `none` for a page you open directly.
- **Requests that change something** also need three things: an `Origin` equal to the app's own address, a JSON body,
  and the header `X-FME-Client: 1`. There is no CORS, so other origins cannot make these requests.
- **Responses** carry a strict Content Security Policy (`default-src 'self'`, no framing), `nosniff`, `no-referrer`
  and other security headers, including on errors.
- **Model output** is shown as plain text, or as Markdown without raw HTML or remote images.
- **Amounts you enter** (prices, budgets and what-if factors) can have at most 30 digits and 20 decimal places, and
  must be below 10^15, so a typo cannot expand into megabytes of digits.

## API keys

- Keys are stored in **Windows Credential Manager** through `keyring`, under the service `FoundryModelEvaluator`. They
  are kept on this computer only and do not roam with your profile. The app refuses insecure keyring backends. An
  in-memory store exists only for automated tests (`FME_KEYRING=memory`).
- The key API is **write-only**: you can set or delete a key, and the app reports only whether one is stored. The key
  field is cleared once the key is saved, password managers are asked not to keep it, and it is never put in browser
  storage.
- Keys must be printable ASCII without spaces.
- A key is read only when a call is made. It is sent only to the host that the run (or the key check) was created
  for:
  - Redirects are never followed; a redirect is reported as an error.
  - Changing an endpoint's host deletes its stored key.
  - A run whose endpoint URL changed after it was created will not send.
  - Endpoints must use HTTPS, except loopback addresses, and loopback endpoints never go through an HTTP proxy.
- As a second line of defence, every key the app has seen is redacted from logs, stored error text, raw provider
  responses, output text and reasoning summaries.

## Data location

The database, logs and instance lock live under `%LOCALAPPDATA%\FoundryModelEvaluator`, which can be overridden with
`FME_DATA_DIR`. The app refuses to start if that folder is inside the repository or under a OneDrive folder. On
Linux and macOS the folder is private to the user (mode 0700). Only one instance can use a data folder at a time.

## Paid-call safety

- **One request per attempt.** No automatic retries anywhere: the SDK is set to `max_retries=0`, and transports do
  not retry.
- **Explicit retries** create new attempt records, and earlier attempts stay in the history and in totals. An
  attempt can be retried only once; to try again, retry the newest attempt.
- **Idempotency keys.** Every paid action (runs, suite runs, judge evaluations, retries) needs one, and it is checked
  atomically in the database. A repeated or concurrent request with the same key returns the first result.
- **Budgets.** Before each call, its worst-case cost is reserved against the run's budget. A sent call that never
  reports usage counts at its full reservation, also after a restart.
- **Cancel** stops in-flight calls and never sends queued ones. Calls that were already sent are shown as possibly
  billed.
- **Restarts.** After a restart, unfinished attempts and evaluations are marked *interrupted*; they are never
  replayed.

## The judge

- The judge sees only the task (its system instructions and user prompt), the rubric, and the answers. It never sees
  model, profile, deployment or endpoint names, costs, latency or reasoning summaries.
- Answers are labelled A, B, … in a random order. The order is stored, and revealed only after judging; until then
  the API does not say which label is which candidate.
- Each answer is wrapped in markers that contain a random code, and the instructions tell the judge to treat
  everything between markers as data. The output is checked strictly against the expected labels, criteria and score
  range; anything else is kept as *malformed*, with the raw output.
- Judge output is parsed in linear time, and output that cannot be read at all (for example, numbers with thousands of
  digits or very deep nesting) is also kept as *malformed*, never lost to an internal error after a paid call.
- Input that is too large for the judge is rejected before sending, never truncated. The size check uses an estimate,
  so the preview also warns when very token-dense text might still exceed the context window; the provider would then
  reject the call, and it is recorded as rejected. A judge that also produced answers in the run is flagged.

## Reviews

Independent reviews were run in two rounds: before the first release, and again after suites, judging and projections
were added. Each round reviewed the token accounting, and the key handling and request security, separately. Every
finding below was fixed with regression tests.

First round, key handling and request security:

- the Claude SDK following redirects
- the run budget not counting cancelled calls that had already been sent
- retry idempotency races
- unredacted raw responses
- control characters accepted in keys
- loopback endpoints going through proxies

Second round, token accounting:

- the judge preview showing a lower worst case than the budget reserves
- comparison averages covering different cells for each candidate, and ignoring the currency
- the judge win rate counting a cell once per run that judged it
- run totals failing after a change of the app currency, and budget spending not shown after a restart
- the smoke test's spending summary leaving out reservations
- projection scenarios that could cross a context-length tier through rounding, silently shrink a cache category
  without a share, trust a partial TTL breakdown, or exclude Claude cache writes that had no TTL

Second round, security:

- judge output parsing that could stall on crafted output, or end in an internal error after the paid call
- amounts entered without size limits, which could produce very large responses and stored values
