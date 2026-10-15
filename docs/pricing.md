# Pricing

Every estimate uses a **rate card**: a price per billing category (uncached input, cache read, cache write and
output), and, for models with long-context pricing, a second tier for large requests. You see and edit the rate card
under **Models & pricing → Rates**.

## Where rates come from

| Source | How it is set | Label |
|---|---|---|
| Azure (auto) | Matched automatically from the cached Azure Retail Prices catalog, only when exactly one meter fits | Azure (auto) |
| Azure (pinned) | You choose a meter from the catalog | Azure (pinned) |
| Manual | You enter a price; a **source URL is required** (an effective date is optional) | Manual |
| Not applicable | The preset or you declare that the category has no separate price; its tokens are billed at the uncached input rate | not applicable |
| Custom rate | An override on top of any source, with a required reason | **Custom rate** (always labelled) |

Claude models are billed through Microsoft Marketplace and are not in the Retail Prices API. Enter their rates
manually, from the Anthropic or Azure Marketplace pricing pages.

## Azure Retail Prices

**Refresh Azure prices** downloads the pay-as-you-go (consumption) prices for every region your endpoints use, in
USD, from `https://prices.azure.com/api/retail/prices`. The default product filters are:

- `serviceName eq 'Foundry Models'`
- `contains(productName,'Azure OpenAI')`

You can change them under **Settings**.

The download is cached in the local database with its retrieval time. The network is used only when you choose
Refresh or run `pricing-check`. For each meter, the latest price whose effective date has already passed is used, so a
price change scheduled for the future does not apply yet.

### Units

The `unitOfMeasure` field decides the unit; `1K`, `1M`, `1K Tokens` and `1M Tokens` are accepted. Hourly and other
units are rejected. If the meter name mentions a different unit than `unitOfMeasure`, the meter is not matched
automatically. Prices are shown per 1M tokens, together with the price as quoted.

### How matching works

Azure meter names are not consistent. Two examples:

```
5.6 terra ShortCo cached Inp Std Gl        gpt-4o-0806-Outp-glbl        FW DeepSeek V4.1 Flash Inp DZ
```

So the matcher first removes the words it understands:

- direction: `Inp`, `Outp`, `Opt`
- cache: `cached`, `Cd Wr`
- deployment type: `glbl`/`Gl`, `DZ`, `rgnl`
- processing tier: `Std`, `PP`
- context tier: `ShortCo`, `LongCo`
- `Text`, and unit words

What remains must equal the profile's **meter terms**, ignoring case, spaces and hyphens. For example, `5.6 terra`
matches the terra meters but not `5.6 luna`, and `gpt-4o` does not match `gpt-4o mini`.

A meter is never matched automatically when any of these holds:

- it mentions Batch, PTU or provisioned, fine-tuning, audio, image, video, or hourly units
- it is missing a required attribute, such as a deployment type
- its deployment type or processing tier differs from the profile's

A category is filled automatically only when exactly one meter qualifies. Otherwise it stays **unknown**, and the
dialog lists the candidates and the reasons, so you can **pin** the right meter or enter a manual rate. Missing a
match is acceptable; a wrong match is not.

A `ShortCo`/`LongCo` pair becomes a two-tier card. Set the profile's **long-context threshold** (for example 272000)
so the app knows where the second tier starts. The tier is then chosen per request, from its total input.

### Checking the live catalog

`scripts\foundry-eval.cmd pricing-check --profile "<name>"` is free. With the app closed, it does two things: it
refreshes the catalog, and shows for every profile and category which meter matched, or why none did. It applies
automatic matches just as **Match Azure prices** does.

## Price snapshots and history

When a run starts, each profile's resolved rate card is frozen into an **immutable price snapshot**. The snapshot
keeps, for each rate:

- the value, in its original unit and per 1M
- the source, and the meter ID or source URL
- the effective date and the retrieval time
- the currency

Database triggers reject any change to a snapshot. Refreshing prices or editing rates later never changes the cost of
a past run. Judge evaluations take their own snapshot.

## Projections

**Models & pricing → Projections** estimates future costs from the usage measured in one or more runs. Each succeeded
request is priced again under your assumptions, then averaged. The result shows the cost per request, per 1,000
requests and per month for a monthly volume you enter.

- **Price basis.** *Original* uses each run's snapshot; *current* uses today's rates. The current basis warns when a
  profile's pricing identity (deployment, tier, region or endpoint) has changed since the run.
- **What-if assumptions.** You can set input and output length multipliers, and the share of input that is cache
  reads or cache writes. Every result lists the assumptions it used, and a share control is disabled when the selected
  models do not bill that category separately.
- **Exclusions.** Requests that failed, have no usage, or would still be incomplete under the scenario are excluded,
  with the counts and reasons shown.

All amounts are **estimated token costs**. Projections cover generation only; judging is excluded.
