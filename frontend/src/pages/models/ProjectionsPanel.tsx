import { useEffect, useMemo, useState } from 'react'

import { useRuns } from '../../api/hooks'
import type { ProjectionRequest, ProjectionResult } from '../../api/types'
import { useDebouncedPost } from '../../api/useDebouncedPost'
import { Badge, Empty, ErrorMessage, Field, Spinner } from '../../components/ui'
import { formatDate } from '../../lib/format'
import { formatMoney, parseDecimal } from '../../lib/money'
import { CostSpreadTable } from './CostSpreadTable'

/** "12.5" (percent) -> "0.125" using decimal string arithmetic; null when empty or invalid. */
export function percentToFraction(text: string): string | null {
  const parsed = parseDecimal(text.trim())
  if (!parsed || parsed.negative) return null
  const scale = parsed.scale + 2
  const digits = parsed.digits.toString().padStart(scale + 1, '0')
  return `${digits.slice(0, -scale)}.${digits.slice(-scale)}`
}

function validMultiplier(text: string): boolean {
  const parsed = parseDecimal(text.trim())
  return parsed !== null && !parsed.negative && parsed.digits > 0n
}

/** A batch price ratio: greater than 0 and at most 1 (the batch price as a fraction of the price used). */
export function validRatio(text: string): boolean {
  const parsed = parseDecimal(text.trim())
  return parsed !== null && !parsed.negative && parsed.digits > 0n && parsed.digits <= 10n ** BigInt(parsed.scale)
}

export function ProjectionsPanel() {
  const runs = useRuns()
  const finished = useMemo(
    () => (runs.data ?? []).filter((r) => !['queued', 'running'].includes(r.run.status)),
    [runs.data],
  )
  const [selected, setSelected] = useState<string[] | null>(null)
  const [basis, setBasis] = useState<'original' | 'current'>('original')
  const [monthly, setMonthly] = useState('10000')
  const [inputMultiplier, setInputMultiplier] = useState('1')
  const [outputMultiplier, setOutputMultiplier] = useState('1')
  const [readShare, setReadShare] = useState('')
  const [writeShare, setWriteShare] = useState('')
  const [writeTtl, setWriteTtl] = useState<'5m' | '1h'>('5m')
  const [batchShare, setBatchShare] = useState('')
  const [batchRatio, setBatchRatio] = useState('')
  useEffect(() => {
    if (selected === null && finished.length) setSelected([finished[0].run.id])
  }, [finished, selected])

  const readFraction = readShare.trim() ? percentToFraction(readShare) : null
  const writeFraction = writeShare.trim() ? percentToFraction(writeShare) : null
  const batchFraction = batchShare.trim() ? percentToFraction(batchShare) : null
  const problems: string[] = []
  if (!validMultiplier(inputMultiplier)) problems.push('The input length multiplier must be a positive number.')
  if (!validMultiplier(outputMultiplier)) problems.push('The output length multiplier must be a positive number.')
  if (readShare.trim() && readFraction === null) problems.push('The cache read share must be a percentage.')
  if (writeShare.trim() && writeFraction === null) problems.push('The cache write share must be a percentage.')
  if (batchShare.trim() && batchFraction === null) problems.push('The batch share must be a percentage.')
  if (batchRatio.trim() && !validRatio(batchRatio))
    problems.push('The batch price ratio must be greater than 0 and at most 1.')
  if (!batchShare.trim() !== !batchRatio.trim()) problems.push('Enter both the batch share and the batch price ratio, or neither.')
  const monthlyCount = Number.parseInt(monthly, 10)
  if (!Number.isInteger(monthlyCount) || monthlyCount < 0) problems.push('Enter a monthly request count.')
  const request: ProjectionRequest | null =
    selected && selected.length && problems.length === 0
      ? {
          run_ids: selected,
          price_basis: basis,
          monthly_requests: monthlyCount,
          what_if: {
            input_multiplier: inputMultiplier.trim(),
            output_multiplier: outputMultiplier.trim(),
            cache_read_share: readFraction,
            cache_write_share: writeFraction,
            cache_write_ttl: writeTtl,
            batch_share: batchFraction,
            batch_rate_ratio: batchRatio.trim() || null,
          },
        }
      : null
  const projection = useDebouncedPost<ProjectionResult>(request ? '/api/projections' : null, request)
  const result = projection.data
  const readApplicable = !result || result.candidates.some((c) => c.cache_read_applicable)
  const writeApplicable = !result || result.candidates.some((c) => c.cache_write_applicable)

  if (runs.isLoading) return <Spinner />
  if (finished.length === 0) return <Empty>Run a comparison or a suite first; projections are based on measured usage.</Empty>
  const toggle = (id: string) =>
    setSelected((current) => {
      const list = current ?? []
      return list.includes(id) ? list.filter((x) => x !== id) : [...list, id]
    })
  return (
    <div className="stack">
      <p className="muted">
        Projects estimated token costs from the usage measured in your runs. Each request is priced on its own (the context-length tier
        is chosen again after the what-if changes), then averaged, and the spread across requests is shown as percentiles. Only
        requests with complete estimates are used.
      </p>
      <div className="card stack">
        <h3>Based on these runs</h3>
        <div className="stack-sm" role="group" aria-label="Source runs">
          {finished.slice(0, 20).map((summary) => (
            <label key={summary.run.id} className="checkbox">
              <input
                type="checkbox"
                checked={(selected ?? []).includes(summary.run.id)}
                onChange={() => toggle(summary.run.id)}
              />
              {summary.run.title || 'Untitled run'} <span className="small muted">{formatDate(summary.run.created_at)}</span>
            </label>
          ))}
        </div>
        <div className="grid-form">
          <Field label="Prices">
            <select value={basis} onChange={(e) => setBasis(e.target.value as 'original' | 'current')}>
              <option value="original">Original: each run's price snapshot</option>
              <option value="current">Current rates</option>
            </select>
          </Field>
          <Field label="Requests per month">
            <input inputMode="numeric" value={monthly} onChange={(e) => setMonthly(e.target.value)} />
          </Field>
        </div>
        <h3>What-if assumptions</h3>
        <p className="small muted">
          A cache share sets that part of each request's input; the rest keeps the measured mix of the other input categories. A
          batch share prices that part of the requests at the batch price ratio and the rest at the full price.
        </p>
        <div className="grid-form">
          <Field label="Input length multiplier" hint="1 = as measured">
            <input inputMode="decimal" value={inputMultiplier} onChange={(e) => setInputMultiplier(e.target.value)} />
          </Field>
          <Field label="Output length multiplier" hint="1 = as measured">
            <input inputMode="decimal" value={outputMultiplier} onChange={(e) => setOutputMultiplier(e.target.value)} />
          </Field>
          <Field
            label="Cache read share of input (%)"
            hint={readApplicable ? 'Empty = as measured' : 'Not billed separately for these models'}
          >
            <input
              inputMode="decimal"
              value={readShare}
              disabled={!readApplicable && !readShare}
              onChange={(e) => setReadShare(e.target.value)}
            />
          </Field>
          <Field
            label="Cache write share of input (%)"
            hint={writeApplicable ? 'Empty = as measured' : 'Not billed separately for these models'}
          >
            <input
              inputMode="decimal"
              value={writeShare}
              disabled={!writeApplicable && !writeShare}
              onChange={(e) => setWriteShare(e.target.value)}
            />
          </Field>
          <Field
            label="Cache write TTL when unknown"
            hint="Claude: for scenario cache writes when neither the run's usage nor its cache setting shows the TTL"
          >
            <select value={writeTtl} disabled={!writeShare.trim()} onChange={(e) => setWriteTtl(e.target.value as '5m' | '1h')}>
              <option value="5m">5 minutes</option>
              <option value="1h">1 hour</option>
            </select>
          </Field>
          <Field label="Batch share of requests (%)" hint="Empty = no batch">
            <input inputMode="decimal" value={batchShare} onChange={(e) => setBatchShare(e.target.value)} />
          </Field>
          <Field
            label="Batch price ratio"
            hint="Batch price ÷ the price used here, for example 0.5. Enter it from your model's batch price; it is not matched."
          >
            <input inputMode="decimal" value={batchRatio} onChange={(e) => setBatchRatio(e.target.value)} />
          </Field>
        </div>
      </div>
      {problems.length > 0 && (
        <div className="callout warn small">
          <ul>
            {problems.map((p) => (
              <li key={p}>{p}</li>
            ))}
          </ul>
        </div>
      )}
      <ErrorMessage error={projection.error} />
      {result && (
        <section className="stack-sm" aria-label="Projection">
          <h3>Estimated token costs (projection)</h3>
          <p className="small">
            <strong>Assumptions:</strong> {result.assumptions.join('; ') || 'none'}
          </p>
          <div className="card table-wrap">
            <table>
              <thead>
                <tr>
                  <th>Candidate</th>
                  <th className="num">Requests used</th>
                  <th className="num">Per request</th>
                  <th className="num">Per 1,000 requests</th>
                  <th className="num">Per month ({result.monthly_requests.toLocaleString('en-US')} requests)</th>
                  <th>Notes</th>
                </tr>
              </thead>
              <tbody>
                {result.candidates.map((item) => (
                  <tr key={item.candidate_key} data-testid="projection-row">
                    <td>
                      {item.label} {item.custom_rates && <Badge tone="custom">custom rates</Badge>}
                    </td>
                    <td className="num">
                      {item.attempts_used}
                      {item.attempts_excluded > 0 && <div className="small muted">{item.attempts_excluded} excluded</div>}
                    </td>
                    <td className="num">{item.cost_per_request ? formatMoney(item.cost_per_request, result.currency) : '—'}</td>
                    <td className="num">{item.cost_per_1k_requests ? formatMoney(item.cost_per_1k_requests, result.currency) : '—'}</td>
                    <td className="num">{item.cost_per_month ? formatMoney(item.cost_per_month, result.currency, 2) : '—'}</td>
                    <td className="small">
                      {item.status === 'unavailable' && <Badge tone="warn">unavailable</Badge>}
                      {[...item.exclusion_reasons, ...item.warnings, ...item.assumptions].map((note) => (
                        <div key={note}>{note}</div>
                      ))}
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
          <CostSpreadTable result={result} />
          {result.notes.map((note) => (
            <p key={note} className="small muted">
              {note}
            </p>
          ))}
        </section>
      )}
    </div>
  )
}
