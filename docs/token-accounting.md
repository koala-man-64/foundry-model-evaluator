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
| Sent, but no usage came back: failed, cancelled or interrupted mid-call | **≥ $0.00, incomplete**. The provider may still bill it. |

## Totals

Run and suite totals add up the estimates. A total is *complete* only if every estimate in it is complete; otherwise
it shows a lower bound and how many estimates are incomplete.

Generation, judging and combined totals are always shown separately, and combined = generation + judging. Judge
evaluations take their own price snapshot, so judging costs follow the same rules.

## Budgets

A run can have a budget. Before each call (including judge calls), the app reserves the call's worst case:

- input: the UTF-8 byte count of the request plus 256 framing tokens, an upper bound on the token count, at the
  highest input-category rate
- output: `max_output_tokens` at the output rate

A call that could exceed the budget is skipped and costs $0. A budget needs complete rates and `max_output_tokens`, so
that the worst case can be bounded.

When a sent call reports no usage, its whole reservation counts as spent. The reservation is saved with the call, so
spending is not under-counted after a restart either.

## Averages and projections

Averages (for example in suite comparisons) use complete estimates only, and the number excluded is shown.

Projections price **each measured request on its own** under the scenario, then average the costs. They never apply
prices to averaged token counts. The scenario can change four things:

- input and output length multipliers
- cache read share
- cache write share
- the price basis: each run's original snapshot, or current rates

After scaling, the context-length tier is chosen again. Shares are not applied to a category that the model does not
bill separately, and the page says so.
