import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import { useState } from 'react'
import { useNavigate, useParams } from 'react-router'

import { del, get, post, put } from '../../api/client'
import { keys, useProfiles, useRates } from '../../api/hooks'
import type { Category, CategoryRate, MatchOut, MeterOut, RatesOut, RefreshOut } from '../../api/types'
import { Badge, Dialog, Empty, ErrorMessage, Field, Spinner } from '../../components/ui'
import { CATEGORY_LABELS, SOURCE_LABELS, formatDate, shortDate } from '../../lib/format'
import { formatMoney, parseDecimal } from '../../lib/money'

type EditMode = 'manual' | 'override' | 'not_applicable' | 'pin'

interface Editing {
  mode: EditMode
  category: Category
  tier: number
}

function RateCell({ entry, currency }: { entry: CategoryRate; currency: string }) {
  if (entry.status === 'not_applicable') {
    return (
      <span>
        <Badge>not applicable</Badge>{' '}
        <span className="small muted">
          {entry.declared_by === 'preset' ? 'preset default' : 'declared by you'} — billed as uncached input
        </span>
      </span>
    )
  }
  if (entry.status === 'unknown' || !entry.rate) {
    return (
      <span>
        <Badge tone="warn">unknown</Badge> <span className="small muted">{entry.reason}</span>
      </span>
    )
  }
  const rate = entry.rate
  const custom = rate.source === 'custom_override'
  return (
    <div className="stack-sm">
      <div className="row">
        <strong>{formatMoney(perMillion(rate.price, rate.unit_tokens), currency, 4)}</strong>
        <span className="small muted">per 1M tokens</span>
        <Badge tone={custom ? 'custom' : 'info'}>{SOURCE_LABELS[rate.source] ?? rate.source}</Badge>
      </div>
      <div className="small muted">
        Quoted as {formatMoney(rate.price, currency, 6)} per {rate.unit_label}
        {rate.meter_name ? ` · meter “${rate.meter_name}”` : ''}
        {rate.effective_date ? ` · effective ${shortDate(rate.effective_date)}` : ''}
        {rate.retrieved_at ? ` · retrieved ${formatDate(rate.retrieved_at)}` : ''}
      </div>
      {rate.source_url && (
        <a className="small" href={rate.source_url} target="_blank" rel="noopener noreferrer">
          Source
        </a>
      )}
      {custom && (
        <div className="small">
          Custom rate: {rate.note}
          {rate.base_price ? ` (underlying rate ${formatMoney(rate.base_price, currency, 6)} per ${rate.base_unit_tokens} tokens)` : ''}
        </div>
      )}
    </div>
  )
}

function perMillion(price: string, unitTokens: number): string {
  // price per unit -> per million, using string math only for the common 1K/1M units.
  if (unitTokens === 1_000_000) return price
  if (unitTokens === 1_000) {
    const parsed = parseDecimal(price)
    if (!parsed) return price
    const scaled = parsed.digits * 1000n
    const text = scaled.toString().padStart(parsed.scale + 1, '0')
    return parsed.scale ? `${text.slice(0, -parsed.scale)}.${text.slice(-parsed.scale)}` : text
  }
  return price
}

function RateDialog({
  editing,
  rates,
  match,
  onClose,
}: {
  editing: Editing
  rates: RatesOut
  match: MatchOut | null
  onClose: () => void
}) {
  const queryClient = useQueryClient()
  const [price, setPrice] = useState('')
  const [unit, setUnit] = useState('1M')
  const [sourceUrl, setSourceUrl] = useState('')
  const [effective, setEffective] = useState('')
  const [note, setNote] = useState('')
  const [search, setSearch] = useState('')
  const profileId = rates.profile_id
  const done = () => {
    queryClient.invalidateQueries({ queryKey: keys.rates(profileId) })
    onClose()
  }
  const save = useMutation({
    mutationFn: async () => {
      if (editing.mode === 'override') {
        return put(`/api/profiles/${profileId}/rates/${editing.category}/override`, {
          tier: editing.tier,
          price,
          unit,
          note,
          source_url: sourceUrl || null,
        })
      }
      if (editing.mode === 'manual') {
        return put(`/api/profiles/${profileId}/rates/${editing.category}`, {
          tier: editing.tier,
          mode: 'manual',
          price,
          unit,
          source_url: sourceUrl,
          effective_date: effective || null,
          note: note || null,
        })
      }
      return put(`/api/profiles/${profileId}/rates/${editing.category}`, { tier: editing.tier, mode: 'not_applicable', note })
    },
    onSuccess: done,
  })
  const pin = useMutation({
    mutationFn: (meter: MeterOut) =>
      put(`/api/profiles/${profileId}/rates/${editing.category}`, {
        tier: editing.tier,
        mode: 'azure_pinned',
        meter_id: meter.meter_id,
        sku_id: meter.sku_id,
      }),
    onSuccess: done,
  })
  const catalog = useQuery({
    queryKey: ['catalog', profileId, search],
    queryFn: () => get<MeterOut[]>(`/api/pricing/catalog?profile_id=${profileId}&q=${encodeURIComponent(search)}`),
    enabled: editing.mode === 'pin' && search.trim().length > 0,
  })
  const label = CATEGORY_LABELS[editing.category] ?? editing.category
  const priceValid = parseDecimal(price) !== null
  const matchCandidates =
    match?.matches.find((m) => m.category === editing.category && (m.context === 'long') === editing.tier > 0)?.candidates ?? []

  const meterRow = (meter: MeterOut, reasons?: string[]) => (
    <tr key={`${meter.meter_id}|${meter.sku_id}`}>
      <td>
        {meter.meter_name}
        <div className="small muted">{meter.product_name}</div>
        {reasons && reasons.length > 0 && <div className="small muted">{reasons.join('; ')}</div>}
      </td>
      <td className="small">
        {meter.category ? (CATEGORY_LABELS[meter.category] ?? meter.category) : '—'} · {meter.deployment_type ?? '?'} ·{' '}
        {meter.processing_tier}
      </td>
      <td className="num">
        {formatMoney(meter.retail_price, rates.currency, 6)} / {meter.unit_of_measure}
      </td>
      <td>
        <button type="button" className="small" onClick={() => pin.mutate(meter)}>
          Pin
        </button>
      </td>
    </tr>
  )

  const titles: Record<EditMode, string> = {
    manual: `Manual rate — ${label}`,
    override: `Custom rate override — ${label}`,
    not_applicable: `Not applicable — ${label}`,
    pin: `Pin an Azure meter — ${label}`,
  }
  return (
    <Dialog
      open
      title={titles[editing.mode]}
      onClose={onClose}
      footer={
        editing.mode === 'pin' ? (
          <button type="button" onClick={onClose}>
            Close
          </button>
        ) : (
          <>
            <button type="button" onClick={onClose}>
              Cancel
            </button>
            <button
              type="button"
              className="primary"
              disabled={
                save.isPending ||
                (editing.mode !== 'not_applicable' && !priceValid) ||
                (editing.mode === 'manual' && !sourceUrl.trim()) ||
                (editing.mode === 'override' && !note.trim())
              }
              onClick={() => save.mutate()}
            >
              Save
            </button>
          </>
        )
      }
    >
      {editing.tier > 0 && <p className="small muted">Long-context tier: requests above {editing.tier} input tokens.</p>}
      {editing.mode === 'not_applicable' && (
        <div className="stack">
          <p>
            Declaring this category not applicable means it has no separate price: its tokens are billed at the uncached input
            rate. Only do this when you know the provider does not charge separately.
          </p>
          <Field label="Note">
            <input value={note} onChange={(e) => setNote(e.target.value)} placeholder="Why it does not apply" />
          </Field>
        </div>
      )}
      {(editing.mode === 'manual' || editing.mode === 'override') && (
        <div className="grid-form">
          <Field label={`Price (${rates.currency})`} error={price && !priceValid ? 'Enter a decimal number' : null}>
            <input inputMode="decimal" value={price} onChange={(e) => setPrice(e.target.value)} placeholder="e.g. 2.50" />
          </Field>
          <Field label="Per">
            <select value={unit} onChange={(e) => setUnit(e.target.value)}>
              <option value="1M">1M tokens</option>
              <option value="1K">1K tokens</option>
            </select>
          </Field>
          <Field label={editing.mode === 'manual' ? 'Source URL (required)' : 'Source URL (optional)'}>
            <input value={sourceUrl} onChange={(e) => setSourceUrl(e.target.value)} placeholder="https://…" />
          </Field>
          {editing.mode === 'manual' && (
            <Field label="Effective date (optional)">
              <input type="date" value={effective} onChange={(e) => setEffective(e.target.value)} />
            </Field>
          )}
          <Field label={editing.mode === 'override' ? 'Label / reason (required)' : 'Note (optional)'}>
            <input value={note} onChange={(e) => setNote(e.target.value)} />
          </Field>
        </div>
      )}
      {editing.mode === 'override' && (
        <p className="small muted">Custom rates are labelled wherever they are used.</p>
      )}
      {editing.mode === 'pin' && (
        <div className="stack">
          {matchCandidates.length > 0 && (
            <>
              <h4>Candidates from the last match</h4>
              <div className="table-wrap">
                <table className="compact" aria-label="Candidates from the last match">
                  <tbody>{matchCandidates.map((c) => meterRow(c.meter, c.reasons))}</tbody>
                </table>
              </div>
            </>
          )}
          <Field label="Search the cached Azure catalog">
            <input value={search} onChange={(e) => setSearch(e.target.value)} placeholder="e.g. terra, 4o, deepseek" />
          </Field>
          {catalog.isFetching && <Spinner />}
          {catalog.data && (
            <div className="table-wrap">
              <table className="compact" aria-label="Catalog search results">
                <tbody>{catalog.data.map((meter) => meterRow(meter, meter.problems))}</tbody>
              </table>
            </div>
          )}
          {catalog.data?.length === 0 && <p className="muted">No meters found. Refresh Azure prices first.</p>}
        </div>
      )}
      <ErrorMessage error={save.error ?? pin.error} />
    </Dialog>
  )
}

export function PricingPanel() {
  const { profileId } = useParams()
  const navigate = useNavigate()
  const profiles = useProfiles()
  const selected = profileId ?? profiles.data?.[0]?.id
  const rates = useRates(selected)
  const queryClient = useQueryClient()
  const [editing, setEditing] = useState<Editing | null>(null)
  const [match, setMatch] = useState<MatchOut | null>(null)
  const [refreshResult, setRefreshResult] = useState<RefreshOut[] | null>(null)
  const invalidate = () => queryClient.invalidateQueries({ queryKey: keys.rates(selected ?? '') })
  const runMatch = useMutation({
    mutationFn: () => post<MatchOut>(`/api/profiles/${selected}/rates/match`),
    onSuccess: (result) => {
      setMatch(result)
      invalidate()
    },
  })
  const refresh = useMutation({
    mutationFn: () => post<RefreshOut[]>('/api/pricing/refresh', {}),
    onSuccess: (result) => {
      setRefreshResult(result)
      queryClient.invalidateQueries({ queryKey: ['rates'] })
      queryClient.invalidateQueries({ queryKey: keys.pricingStatus })
    },
  })
  const setAuto = useMutation({
    mutationFn: ({ category, tier }: { category: Category; tier: number }) =>
      put(`/api/profiles/${selected}/rates/${category}`, { tier, mode: 'azure_auto' }),
    onSuccess: invalidate,
  })
  const clearOverride = useMutation({
    mutationFn: ({ category, tier }: { category: Category; tier: number }) =>
      del(`/api/profiles/${selected}/rates/${category}/override?tier=${tier}`),
    onSuccess: invalidate,
  })

  if (profiles.isLoading) return <Spinner />
  if (!profiles.data?.length) return <Empty>Add a model profile to configure its rates.</Empty>
  const data = rates.data
  return (
    <div className="stack">
      <div className="row-between">
        <label className="row">
          Profile
          <select value={selected} onChange={(e) => navigate(`/models/pricing/${e.target.value}`)}>
            {profiles.data.map((p) => (
              <option key={p.id} value={p.id}>
                {p.name}
              </option>
            ))}
          </select>
        </label>
        <div className="row">
          <button type="button" onClick={() => refresh.mutate()} disabled={refresh.isPending}>
            {refresh.isPending ? 'Refreshing…' : 'Refresh Azure prices'}
          </button>
          <button type="button" className="primary" onClick={() => runMatch.mutate()} disabled={runMatch.isPending || !selected}>
            Match Azure prices
          </button>
        </div>
      </div>
      <ErrorMessage error={rates.error ?? runMatch.error ?? refresh.error ?? setAuto.error ?? clearOverride.error} />
      {refreshResult && (
        <div className="callout small">
          {refreshResult.length === 0 && 'No regions in use: set a region on your endpoints.'}
          {refreshResult.map((r) => (
            <div key={r.region}>
              {r.region}: {r.status} ({r.item_count} price items){r.error ? ` — ${r.error}` : ''}
              {r.warnings.map((w) => (
                <div key={w} className="muted">
                  {w}
                </div>
              ))}
            </div>
          ))}
        </div>
      )}
      {data && (
        <div className="card stack">
          <div className="row-between">
            <div className="small muted">
              Region {data.region || '—'} · {data.currency} ·{' '}
              {data.catalog
                ? `Azure prices from ${data.catalog.finished_at ? formatDate(data.catalog.finished_at) : 'an earlier refresh'} (${data.catalog.item_count ?? 0} items)`
                : 'Azure prices not fetched yet'}
              {data.last_refresh_error && (
                <div className="warn-text" data-testid="last-refresh-error">
                  Last refresh failed
                  {data.last_refresh_error.finished_at ? ` ${formatDate(data.last_refresh_error.finished_at)}` : ''}:{' '}
                  {data.last_refresh_error.error}
                </div>
              )}
            </div>
          </div>
          {match && (
            <div className="callout small">
              <strong>Match results</strong>
              <ul>
                {match.matches.map((m) => (
                  <li key={`${m.category}-${m.context}`}>
                    {CATEGORY_LABELS[m.category] ?? m.category}
                    {m.context === 'long' ? ' (long context)' : ''}: {m.status} — {m.message}
                  </li>
                ))}
                {match.notes.map((n) => (
                  <li key={n}>{n}</li>
                ))}
              </ul>
            </div>
          )}
          {data.card.explanations && data.card.explanations.length > 0 && (
            <div className="callout warn small">{data.card.explanations.join(' ')}</div>
          )}
          {data.card.tiers.map((tier) => (
            <div key={tier.above_input_tokens} className="table-wrap">
              <h3>{tier.above_input_tokens ? `Long-context tier (above ${tier.above_input_tokens} input tokens)` : 'Rates'}</h3>
              <table>
                <thead>
                  <tr>
                    <th>Category</th>
                    <th>Rate</th>
                    <th>Change source</th>
                  </tr>
                </thead>
                <tbody>
                  {data.categories.map((category) => {
                    const entry = (tier.categories?.[category] as CategoryRate | undefined) ?? { status: 'unknown', reason: 'No rate configured.' }
                    const isOverride = entry.rate?.source === 'custom_override'
                    const t = tier.above_input_tokens ?? 0
                    return (
                      <tr key={category} data-testid={`rate-${category}-${t}`}>
                        <td>{CATEGORY_LABELS[category] ?? category}</td>
                        <td>
                          <RateCell entry={entry as CategoryRate} currency={data.currency} />
                        </td>
                        <td>
                          <div className="row">
                            <button type="button" className="small" onClick={() => setAuto.mutate({ category, tier: t })}>
                              Azure auto
                            </button>
                            <button type="button" className="small" onClick={() => setEditing({ mode: 'pin', category, tier: t })}>
                              Pin meter
                            </button>
                            <button type="button" className="small" onClick={() => setEditing({ mode: 'manual', category, tier: t })}>
                              Manual
                            </button>
                            <button
                              type="button"
                              className="small"
                              onClick={() => setEditing({ mode: 'not_applicable', category, tier: t })}
                            >
                              Not applicable
                            </button>
                            {isOverride ? (
                              <button type="button" className="small" onClick={() => clearOverride.mutate({ category, tier: t })}>
                                Clear override
                              </button>
                            ) : (
                              <button type="button" className="small" onClick={() => setEditing({ mode: 'override', category, tier: t })}>
                                Override
                              </button>
                            )}
                          </div>
                        </td>
                      </tr>
                    )
                  })}
                </tbody>
              </table>
            </div>
          ))}
          <p className="small muted">
            Claude models are billed through Microsoft Marketplace and are not in the Azure Retail Prices API: enter manual rates
            from the Anthropic or Azure Marketplace pricing pages, with the source URL.
          </p>
        </div>
      )}
      {editing && data && <RateDialog editing={editing} rates={data} match={match} onClose={() => setEditing(null)} />}
    </div>
  )
}
