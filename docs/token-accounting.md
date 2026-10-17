# Token accounting

This page describes how the app turns a provider's usage report into an **estimated token cost**. Estimates come
from token counts and rates. They are not bills.

The two rules behind everything here:

1. **Nothing is guessed.** A count the provider did not report shows as *not reported* and is never treated as zero.
2. **Unknown means incomplete, never zero.** If a count or a rate is unknown, the estimate is shown as a lower bound
   (and an upper bound when one exists), with the reasons. A request that was **never sent** is the only case that
   costs exactly $0.

## Normalized usage

Each API reports usage differently. The app maps every report onto the same facts:

| Fact | OpenAI Responses | Chat Completions | Claude Messages |
|---|---|---|---|
| Total input | `input_tokens` | `prompt_tokens` | uncached + cache read + cache write |
| Cache read | `input_tokens_details.cached_tokens` | `prompt_tokens_details.cached_tokens` | `cache_read_input_tokens` |
| Cache write | `input_tokens_details.cache_write_tokens` | `prompt_tokens_details.cache_write_tokens` | `cache_creation_input_tokens`, with the 5-minute/1-hour split from `cache_creation` |
| Uncached input | total − reads − writes | total − reads − writes | `input_tokens` |
| Output (billed) | `output_tokens` | `completion_tokens` | `output_tokens` |
| Reasoning (part of output) | `output_tokens_details.reasoning_tokens` | `completion_tokens_details.reasoning_tokens` | thinking tokens, if reported |

Two differences matter for cost:

- **OpenAI-compatible APIs** count cached and cache-write tokens *inside* the total input, and reasoning tokens
  *inside* the output. They are never charged twice.
- **Claude** reports uncached input, cache reads and cache writes *separately*, and its total is their sum. The TTL
  split of cache writes (5 minutes or 1 hour) is kept, because the two are priced differently.

Unknown non-zero token fields, audio or image tokens, and inconsistent counts (for example cache reads larger than the
total) are flagged and make the estimate incomplete.

## Billing categories

Each estimate prices disjoint categories: uncached input, cache read, cache write (OpenAI), cache write 5 minutes and
cache write 1 hour (Claude), and output. Each category costs tokens × price ÷ unit size. The arithmetic uses exact
decimals; amounts are rounded only for display.

Each category's rate is one of:

- **priced**: from Azure, a manual entry or a custom override
- **not applicable**: declared by the profile's preset or by you. The tokens are billed at the uncached input rate,
  and the declaration is shown.
- **unknown**

A rate is never marked *not applicable* just because no Azure meter was found.

## Worked examples

All prices below are per 1M tokens.

### 1. OpenAI Responses with cache reads, cache writes and reasoning

Usage: `input_tokens` 12,000 (of which `cached_tokens` 8,000 and `cache_write_tokens` 2,000), and `output_tokens`
1,500 (of which `reasoning_tokens` 1,000). Rates: uncached $2.50, cache read $0.25, cache write $3.125, output $15.

| Category | Tokens | Cost |
|---|---:|---:|
| Uncached input | 12,000 − 8,000 − 2,000 = 2,000 | $0.005 |
| Cache read | 8,000 | $0.002 |
| Cache write | 2,000 | $0.00625 |
| Output (the 1,000 reasoning tokens are already inside) | 1,500 | $0.0225 |
| **Estimated token cost** | | **$0.03575** |

### 2. The same request when cache writes are not reported

Some deployments omit `cache_write_tokens`. That leaves 12,000 − 8,000 = 4,000 tokens that are either uncached input
or cache writes.

- If all 4,000 are uncached input at $2.50, they cost $0.010. That gives the lower bound: $0.0345.
- If all 4,000 are cache writes at $3.125, they cost $0.0125. That gives the upper bound: $0.0370.

The estimate shows as **≥ $0.0345 (≤ $0.0370), incomplete: split between uncached input and cache write not
reported**.

If you know your deployment never bills cache writes separately, you can declare the category *not applicable*. That
choice is recorded and labelled.

### 3. Claude with a TTL split

Usage: `input_tokens` 1,000 (uncached), `cache_read_input_tokens` 6,000, and `cache_creation_input_tokens` 3,000
(5 minutes 1,000, 1 hour 2,000). Then `output_tokens` 500. Rates: input $3, cache read $0.30, write 5 minutes $3.75,
write 1 hour $6, output $15.

| Category | Tokens | Cost |
|---|---:|---:|
| Uncached input | 1,000 | $0.003 |
| Cache read | 6,000 | $0.0018 |
| Cache write (5 min) | 1,000 | $0.00375 |
| Cache write (1 hour) | 2,000 | $0.012 |
| Output | 500 | $0.0075 |
| **Estimated token cost** (total input 10,000) | | **$0.02805** |

If the TTL split is missing, there are two cases:

- **The request asked for a single TTL** (for example 1 hour). Then all 3,000 writes are priced at that rate, which
  gives $0.0303. The estimate is labelled *cache-write TTL inferred from the request*.
- **No TTL was requested.** Then the estimate is incomplete, between ≥ $0.02355 and ≤ $0.0303.

### 4. Long-context tiers

Some models charge more when a request is large. For example, a profile with a long-context threshold of 272,000
input tokens uses higher rates for requests above it. The tier is chosen per request, from that request's total
input.

So 300,000 input tokens and 2,000 output tokens at the long-context rates ($5 input, $22.50 output) cost
$1.50 + $0.045 = **$1.545**. If the total input is not reported, the tier is unknown and the estimate is incomplete.

### 5. Requests without usage

| Situation | Estimated token cost |
|---|---|
| Never sent: cancelled while waiting, skipped by the budget, or interrupted before sending | **$0.00 (not sent)** |
| Sent, but no usage came back: failed, cancelled or interrupted mid-call | **≥ $0.00, incomplete**. The provider may still bill it. In a run with a budget, the call's reservation is shown as its upper bound. |

## Totals

Run and suite totals add up the estimates. A total is *complete* only if every estimate in it is complete; otherwise
it shows a lower bound and how many estimates are incomplete.

Generation, judging and combined totals are always shown separately, and combined = generation + judging. Judge
evaluations take their own price snapshot, so judging costs follow the same rules.

A run's totals use the currency the run was priced in, even after you change the app's currency. Amounts are never
converted: an estimate in another currency (for example, judging done after a currency change) is left out, and the
total becomes incomplete and says how many estimates it left out.

## Budgets

A run can have a budget. Before each call (including judge calls), the app reserves the call's worst case:

- input: the UTF-8 byte count of the exact request body that is sent (prompt, settings and any schema, as JSON) plus
  256 framing tokens, an upper bound on the token count, at the highest input-category rate
- output: `max_output_tokens` at the output rate

A call that could exceed the budget is skipped and costs $0. A budget needs complete rates and `max_output_tokens`, so
that the worst case can be bounded.

The worst case is as good as the rate card. A category marked *not applicable* counts at the input rate, as the preset
or you declared. Providers can also add a few instructions of their own, for example for structured output; the 256
framing tokens and the gap between bytes and tokens cover these in practice.

When a sent call reports no usage, its whole reservation counts as spent. The reservation is saved with the call, so
spending is not under-counted after a restart either.

The run and judge previews show the same worst case that is reserved: they bound the whole request body of every
call, including any structured-output schema, and the run preview shows the largest per-call worst case of each
candidate. It warns when that worst case is more than what is left of the budget; evaluations that do
not fit are skipped and cost nothing. Judging a run that has a budget needs judge rates in the run's currency. A
position-swap pass (below) is previewed, bounded and reserved in exactly the same way.

## Comparisons

A suite comparison matches **cells**: the same prompt text and trial number, across the selected runs. A cell counts
as *matched* when every candidate has a succeeded answer in it, and the page shows how many cells matched.

- **Common cells.** Each metric is averaged within a cell first, so a cell repeated across runs counts once. The cell
  values are then averaged over the matched cells where every candidate has that value. The averages side by side
  therefore always cover the same prompts and trials, and each value shows how many cells it used.
- **Costs** use complete estimates in the app's currency. Incomplete estimates and estimates in another currency are
  left out and counted.
- **Judge win rate.** The share of the judge's preferences, averaged per cell in the same way. A tie among *n* judged
  answers gives each one 1/*n*, so the shares of one evaluation always add up to 1.
- **Judge scores must share a basis.** Judge scores and wins are only averaged when every evaluation in the cells being
  averaged used the same rubric version, the same judge (profile, deployment, model name and version, API style,
  structured output) and the same scoring settings (temperature, top_p, top_k, effort, thinking mode and budget).
  Otherwise a 1–5 and a 1–10 rubric, or two different judges, would be averaged together, so the judge averages and
  win rates are shown as withheld and a note says why. Re-judging with a larger `max_output_tokens`, other display
  options or a seed keeps the basis.
- **Every judge evaluation counts.** Judge scores and wins aggregate all succeeded evaluations of a prompt and trial
  on one basis, instead of using only the newest one (see *Judge evidence* below).
- **Only answers count as succeeded.** A response that asks to call a tool, or that has no answer text (for example a
  reasoning model that used all of `max_output_tokens` before answering), is recorded as a failed attempt with the
  reason. It keeps its usage and estimated cost, because the provider billed it, but it is never matched or judged.
  Comparisons count these billed failures per candidate, projections list them as "billed, but not an answer", and
  **Retry failed** leaves them out because the same request would most likely repeat; retry one explicitly instead.
- **Failures** count finished attempts that did not succeed. Attempts that have not finished are shown separately.

### Judge evidence

Re-judging used to replace evidence: comparisons and calibration used only the newest succeeded evaluation of each
prompt and trial, and silently ignored the rest. Now every succeeded evaluation is kept and used.

- **One basis per prompt and trial.** The succeeded evaluations of each prompt and trial of a run are grouped by judge
  basis (rubric version, judge and scoring settings, as above). The basis with the **most evaluations** is used; on a
  tie, the one judged **most recently** (then a fixed order, so the choice never depends on how rows are read).
  Evaluations on another basis are **left out, never mixed in**, and a note says how many. Re-judging once with a new
  rubric therefore still switches to it (one each, and the newer wins), but one re-judge no longer outweighs two
  evaluations on the original basis.
- **Only the same answers are averaged together.** An evaluation made while one answer had failed judged fewer
  answers than one made after that answer was retried; their preference shares are not comparable (each set sums to
  1). So the evaluations used must have judged exactly the same answers, and the ones that judged the **most
  answers** come first, before the basis rule above. That is normally every current answer, because the set only
  grows when a failed answer is retried. Evaluations of fewer answers are left out with a note.
- **Averages.** An answer's judge score is the mean of its weighted overall scores over that basis's evaluations. A
  candidate's win share in a cell is averaged over the evaluations of each run first, then over runs, so a run judged
  three times weighs as much as one judged once. Comparisons show how many evaluations are behind each judge average
  and their **spread**: how far one answer's score moved between evaluations (highest minus lowest), averaged over the
  cells judged more than once. A single evaluation has no spread: that is unknown, not zero.
- **Retried answers.** An evaluation of an answer that was retried since judged text that is no longer shown. It is
  left out and counted.
- **Across runs**, each run's prompts and trials choose their basis separately, and the basis rule above still holds:
  if the cells being averaged end up on different bases, judge averages and win rates are withheld.
- **What did not change.** The run view's judge score and the export's `judge_weighted_overall` column are still the
  latest single evaluation, because they name its evaluation and label; the run view adds the mean that comparisons
  use when it differs. The aggregate only reads stored evaluations, so it costs nothing and never calls a model.

### Position swaps

The judge sees the answers in a random order. That spreads position bias over the answers but does not measure it.
A **swap pass** measures it:

- From a succeeded evaluation, **Swap order and re-judge** sends the same answers to the same judge, rubric version
  and settings, in exactly the reverse order (label A is always the answer shown first). With the scope **every prompt
  and trial of the run judged this way**, it swaps the most recent evaluation on that basis in each prompt and trial
  that has no swap on that basis yet, so pressing it again never pays for the same prompt and trial twice (swap
  another evaluation on its own if you want more pairs).
- It is a paid action like any judging: never a default, run only when you confirm its preview (calls, worst-case
  cost, budget), sent with an idempotency key, and never retried automatically. A swap that fails stays failed;
  swapping that evaluation again takes a new explicit request. An evaluation cannot be swapped when it already has a
  queued, running or succeeded swap, is a swap itself, judged fewer than two answers, or judged an answer that was
  retried since. If the judge profile's deployment, model, API style or structured output changed since, the swap is
  refused, because it would not be like for like; this is checked again when the swap is written, so an edit after
  the preview stops it too. The confirm button only accepts a preview of exactly the scope shown.
- A swap is on the same basis as its source, so its scores join the aggregate above and comparisons average both
  orders.
- **Judge vs. your scores** reports, per judge basis, the swap pairs (an evaluation and its swap, both succeeded):
  **flipped** when the judge preferred a different answer once the order was reversed (with two answers, it preferred
  the same *position* both times), and **consistent** when it kept the same answer, or a tie both times. A tie in only
  one of the two is neither. The flip share is the position-bias statistic; it is shown with its counts, because a
  handful of pairs says little.
- Pairs follow the scores' exclusions: a pair in which the judge also produced an answer, or whose answers were
  retried since, is left out.
- With **three or more answers**, reversing the order moves every answer except the middle one of an odd count. A
  pair whose first evaluation preferred that middle answer cannot tell position from content and is left out. A flip
  then means the preference moved to another answer, which is not always the mirrored position, so the flip share is
  clearest with two answers.

### Checks

A prompt can carry up to 10 deterministic checks. They run locally on each succeeded answer, every time results are
shown, so they cost nothing and always agree with the checks frozen into the run.

| Check | Passes when |
|---|---|
| Contains / Does not contain | the answer contains (or does not contain) the text, case-insensitive unless set |
| Equals | the whole answer equals the text, ignoring leading, trailing and repeated whitespace |
| Matches pattern | the regular expression is found in the answer (case-insensitive unless set), within a time limit |
| Number | the last number in the answer (or any number) is within the tolerance of the value; `1,234.5` reads as 1234.5 |
| JSON schema | the answer, optionally inside one ``` fence, is JSON that matches the schema |

- An answer **passes** when every check of its prompt passes. Comparisons show the share of answers that pass, per
  cell and then over the common cells, with the same 95% range and baseline difference as the other metrics. It is a
  separate score: it is never blended with manual or judge scores.
- A check that cannot be evaluated on a particular answer (for example JSON containing an integer of thousands of
  digits, which the parser refuses) shows **could not check**. That is unknown: it is neither a pass nor a fail, and
  the answer is left out of the pass rate.
- Numbers: a sign counts only when it does not follow a letter, digit or dot, so `10-20` reads as 10 and 20, and a
  date such as `2024-01-05` reads as 2024, 1 and 5. `.5` reads as 0.5, and the Unicode minus sign is understood.
  Numbers with an exponent beyond ±1000 are skipped. Text comparisons normalise Unicode, so a composed and a
  decomposed accent compare equal.
- In JSON schemas, `true` is not equal to `1` and `false` is not equal to `0` in `const` and `enum`, and `NaN` or
  `Infinity` in an answer is not valid JSON.
- Checks that could not run as written are refused when the suite is saved, never skipped later: a JSON schema
  keyword this checker does not implement (it supports `type`, `properties`, `required`, `additionalProperties: false`,
  `items`, `enum`, `const`, `minItems`/`maxItems`, `minLength`/`maxLength` and `minimum`/`maximum`), a keyword value of
  the wrong type (such as `"minimum": "5"`), `NaN` or `Infinity` in `const` or `enum`, or a number given as a float.
  A schema is limited to 500 sub-schemas, 200 `enum` values and 200 `properties` per object, so checking a large answer
  stays fast.
- **Matches pattern** uses the `regex` package, not Python's `re`, because `re` has no time limit. Patterns are up
  to 500 characters; lookarounds and backreferences work. A pattern can come from an imported prompt set and an
  answer from any model, and a hostile pattern can take seconds and gigabytes to compile (`(((a{200}){200}){200})`)
  or overflow the native stack while searching. So the app never compiles or searches a pattern itself; one worker
  process does both, with its memory capped at 512 MB (a Windows job object, or `RLIMIT_AS` on Linux). Limits:
  - When a suite is saved or a run is created, each new pattern is compiled once in the worker. A pattern that is
    invalid, needs too much memory or takes longer than 0.05 s to compile is refused with a message (ordinary
    patterns take well under a millisecond). Vetting one save stops after 10 s.
  - Each search stops after 0.25 s and shows **could not check** (unknown) for that answer only. A worker that stops
    answering, overflows its stack or reaches its memory cap is replaced, and only that answer is affected.
  - Each view (run detail, comparison) has 2 s for pattern checks in total, and an export 30 s. Requests take
    turns with the worker; a view does not wait for its turn longer than it has left, and a save not longer than its
    10 s. When a view runs out, the remaining pattern checks show **not checked yet** (unknown). Results are kept per
    answer and pattern while the app runs, so reloading continues where the last view stopped. A **could not check**
    is tried again after 10 minutes, in case the machine was only busy.
  - Nothing not checked yet is passed off as complete. The run page says **not checked yet** with "reload to
    continue"; a comparison leaves those answers out of the checks pass rate with a note saying how many (and,
    separately, how many could not be checked); a baseline gate on the checks pass rate is inconclusive while any
    answer is not checked yet; an export marks them `not_checked`.
- A **reference answer** is shown to the judge as a separate, delimited part of the task, with the instruction that it
  may be imperfect and that a correct answer need not match its wording. The reference and the checks are not part of
  a prompt's identity, so editing them keeps cells matched across runs; a run keeps the reference and checks it was
  created with.
- A cell repeated across runs must have been graded the same way. A cell whose runs used different checks is left
  out of the checks pass rate, and one whose runs gave the judge different reference answers is left out of the judge
  scores and win rates. A note says how many cells were left out.

### Uncertainty

A small difference in an average can be noise, so every average also shows a **95% range across prompts**:

- **Prompts are the unit of evidence.** The trials of a prompt are averaged first, because they repeat the same task;
  counting ten trials of three prompts as thirty samples would make the range look far tighter than the evidence is.
- The range is a Student-t interval of the mean of the per-prompt values over the metric's common cells. It needs at
  least **3 prompts**, and it is wide with few prompts: that is the honest answer, not a defect. Cost ranges are
  computed in exact decimals and never go below zero; win-rate ranges stay between 0 and 100%, manual scores between
  1 and 5, and judge scores within the rubric's scale.
- The range is centred on the mean of the per-prompt values. When prompts have different numbers of matched trials,
  that can differ slightly from the cell average shown above it, and the range then says which mean it is around.
- **No range for identical values.** When every prompt gives the same value (three straight wins, a constant score
  gap), a t-interval would shrink to a single point and claim a certainty the data cannot give, so no range is
  shown.
- **Baseline.** Pick a candidate as the baseline and every other candidate shows its paired difference to it (the
  difference per prompt, then the same interval). The verdict is *higher* or *lower* only when the whole interval is
  on one side of zero; otherwise it is *cannot tell*. Below 3 shared prompts it says *too few prompts*. When every
  prompt shows the same difference, there is no interval and the verdict follows an exact sign test: a direction only
  from 6 prompts all on one side (two-sided p < 0.05), otherwise *cannot tell*.
- The ranges are not corrected for comparing many candidates at once, so with many candidates expect the occasional
  "difference" by chance.

### Judge vs. your scores

The Scoring page shows how closely the judge agrees with your manual scores on the same answers, per judge basis
(rubric version, judge and scoring settings). It reads stored scores only and costs nothing.

- Judge scores are rescaled from the rubric's scale to 1–5 before they are compared with yours.
- **Rank agreement** is Spearman's correlation (ties share their average rank). It is shown from 5 answers scored by
  both. Its approximate 95% interval counts **prompts**, not answers, because answers to one prompt are related; it
  appears from 5 prompts, and not for perfect agreement.
- **Within 1 point** is the share of answers where the judge is within one point of you; **average difference** is
  judge minus you, so a positive value means the judge scores higher.
- An answer judged more than once on a basis is compared with you **once, at its mean score**, so repeated judging
  does not count as more answers. Each basis has its own row, so nothing is left out here for using another basis.
- **Same top pick** counts prompts and trials where the judge's pick matches the answer you scored highest (only when
  exactly one answer had your top score). The judge's pick is the answer with the highest mean preference share over
  that basis's evaluations; when evaluations disagree so that answers are level, there is no pick.
- Evaluations in which the judge also produced one of the answers are left out and counted.
- **Order swap** shows the swap pairs of each basis: how many flipped and how many were consistent (see *Position
  swaps*).
- Your scores are one person's view, not ground truth: the panel tells you whether the judge agrees with *you*.
- During blind review the panel is hidden, because the comparison would reveal the judge's scores.

## Projections

Projections price **each measured request on its own** under the scenario, then average the costs. They never apply
prices to averaged token counts. The scenario can change these things:

- input and output length multipliers
- cache read share
- cache write share
- the cache-write TTL to assume when it is unknown (Claude)
- a batch share of requests and a batch price ratio
- the price basis: each run's original snapshot, or current rates

Each request's input is scaled and rounded once, and the context-length tier is chosen again from that total. A share
sets its category's part of the total. The rest keeps the measured mix of the other categories, and the result lists
this as an assumption. The parts are rounded so that they add up to the total exactly, so rounding can never push a
request over a tier threshold.

Shares are not applied to a category that the model does not bill separately, and the page says so.

Claude prices cache writes by TTL:

1. A measured TTL split is kept when the write volume changes.
2. Otherwise the request's cache setting decides, as for measured usage.
3. When neither shows the TTL, the scenario's assumption is used (5 minutes unless you choose 1 hour), and it is
   listed with the result.

A partial TTL breakdown from the provider is never trusted, so such requests stay incomplete and are excluded.

Requests that cannot be priced completely under the scenario are excluded. Exclusions with the same cause are counted
together.

### Batch what-if

The batch share and the batch price ratio go together: one without the other is rejected. The share is from 0 to 1 and
the ratio is above 0 and at most 1. Each request's scenario cost `c` becomes

```
(1 − share) × c + share × ratio × c
```

so 40% of requests at a ratio of 0.5 make every request cost 0.8 × `c`. The blend is applied to every request rather
than sending some requests to batch at random, so the percentiles below are of the blended cost. That is exact for
the average and for the "if every request cost the p50 / p95" months, which are linear in the cost. The ratio is
entered, not matched (batch meters are always excluded from matching), and it is listed as an assumption. Batch
turnaround, quotas and caching rules are not modelled.

### Spread of the cost per request

The average cost per request hides the tail: a few long requests can cost many times the typical one. Projections
therefore also show the **p50, p90, p95 and the largest** cost per request.

- They are **nearest-rank** percentiles: sort the per-request costs, and the p-th percentile is the value at rank
  ⌈p/100 × n⌉. It is always one of the measured requests' costs, never an interpolation, so it is an exact decimal.
  With 10 requests the p50 is the 5th, the p90 the 9th and the p95 the 10th (the largest). Below 20 requests the
  p95 is always the largest request.
- They cover the same requests as the average: only requests whose scenario estimate is complete. The table says how
  many requests they are over and how many were excluded. A candidate with no complete estimate has no spread; it is
  shown as unknown, not as zero.
- **Below 30 priced requests** the percentiles carry a warning: the tails then rest on a handful of requests.
  Trials of the same prompt count as separate requests, so 30 requests from three prompts describe those three
  prompts, not your traffic.
- The two monthly figures are the monthly volume times the p50, and times the p95. They answer "what if every
  request cost this much", so they are labelled that way. They are **not** a forecast range or an interval for the
  monthly bill: a month of real traffic mixes cheap and expensive requests, and its expected cost is the average
  times the volume.
