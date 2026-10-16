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
not fit are skipped and cost nothing. Judging a run that has a budget needs judge rates in the run's currency.

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
- **Only answers count as succeeded.** A response that asks to call a tool, or that has no answer text (for example a
  reasoning model that used all of `max_output_tokens` before answering), is recorded as a failed attempt with the
  reason. It keeps its usage and estimated cost, because the provider billed it, but it is never matched or judged.
  Comparisons count these billed failures per candidate, projections list them as "billed, but not an answer", and
  **Retry failed** leaves them out because the same request would most likely repeat; retry one explicitly instead.
- **Failures** count finished attempts that did not succeed. Attempts that have not finished are shown separately.

### Checks

A prompt can carry up to 10 deterministic checks. They run locally on each succeeded answer, every time results are
shown, so they cost nothing and always agree with the checks frozen into the run.

| Check | Passes when |
|---|---|
| Contains / Does not contain | the answer contains (or does not contain) the text, case-insensitive unless set |
| Equals | the whole answer equals the text, ignoring leading, trailing and repeated whitespace |
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
  the wrong type (such as `"minimum": "5"`), or a number given as a float.
- There is **no regular-expression check**. Python's regular expressions have no time limit and block the whole app
  while they run, so one slow pattern on one answer would freeze every view of a run whose checks are frozen. A
  pattern check needs a regex engine with a time budget, which is a new dependency.
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
- **Same top pick** counts evaluations where the judge preferred the answer you scored highest (only when exactly
  one answer had your top score).
- Evaluations in which the judge also produced one of the answers are left out and counted.
- Your scores are one person's view, not ground truth: the panel tells you whether the judge agrees with *you*.
- During blind review the panel is hidden, because the comparison would reveal the judge's scores.

## Projections

Projections price **each measured request on its own** under the scenario, then average the costs. They never apply
prices to averaged token counts. The scenario can change these things:

- input and output length multipliers
- cache read share
- cache write share
- the cache-write TTL to assume when it is unknown (Claude)
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
