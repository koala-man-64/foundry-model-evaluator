import { useEffect, useState } from 'react'

import { useComparison } from '../../api/hooks'
import type { ComparisonCandidate } from '../../api/types'
import { ComparisonCharts } from '../../components/charts/ComparisonCharts'
import { Badge, Empty, ErrorMessage, Spinner } from '../../components/ui'
import { formatMs } from '../../lib/format'
import { formatMoney } from '../../lib/money'
import { DifferenceLine, IntervalLine } from './uncertainty'

function number(value: number | null | undefined, digits = 0): string {
  if (value === null || value === undefined) return '—'
  return value.toLocaleString('en-US', { maximumFractionDigits: digits, minimumFractionDigits: digits })
}

function cells(count: number): string {
  return `${count} cell${count === 1 ? '' : 's'}`
}

function CostCell({ item, currency }: { item: ComparisonCandidate; currency: string }) {
  const excluded = [
    item.cost_excluded > 0 ? `${item.cost_excluded} incomplete excluded` : '',
    item.cost_other_currency > 0 ? `${item.cost_other_currency} in another currency excluded` : '',
  ].filter(Boolean)
  return (
    <span title={[`${cells(item.cost_cells)} averaged`, ...excluded].join('; ')}>
      {item.avg_cost ? formatMoney(item.avg_cost, currency) : <span className="muted">—</span>}
      {item.avg_cost && item.custom_rates && (
        <>
          {' '}
          <Badge tone="custom">custom rates</Badge>
        </>
      )}
      <div className="small muted">{cells(item.cost_cells)}</div>
      <IntervalLine kind="cost" interval={item.cost_interval} shown={item.avg_cost} currency={currency} />
      <DifferenceLine kind="cost" difference={item.vs_baseline?.cost} currency={currency} />
      {excluded.map((note) => (
        <div key={note} className="small muted">
          {note}
        </div>
      ))}
    </span>
  )
}

const WITHHELD = 'Withheld: the judge scores use different rubrics, judges or judge settings'

export function ComparisonTable({ runIds, charts = true }: { runIds: string[]; charts?: boolean }) {
  const [baseline, setBaseline] = useState('')
  const comparison = useComparison(runIds, true, baseline)
  const returned = comparison.data?.baseline ?? null
  // A baseline that is no longer among the compared candidates (other runs selected) is dropped.
  useEffect(() => {
    if (baseline && !comparison.isPlaceholderData && comparison.data && returned === null) setBaseline('')
  }, [baseline, returned, comparison.data, comparison.isPlaceholderData])
  if (runIds.length === 0) return <Empty>Select one or more runs to compare.</Empty>
  if (comparison.isLoading) return <Spinner />
  if (comparison.error) return <ErrorMessage error={comparison.error} />
  const data = comparison.data
  if (!data) return null
  const withheld = data.judge_bases > 1
  return (
    <div className="stack-sm" aria-label="Comparison">
      {charts && <ComparisonCharts comparison={data} />}
      <p className="small" data-testid="coverage">
        Coverage: <strong>{data.cells_matched}</strong> of {data.cells_total} cells (prompt × trial) have a usable answer from every
        candidate. Each average uses only the cells where every candidate has that value, so the averages side by side cover the same
        prompts and trials; the count under each value says how many. Failures count all cells.
      </p>
      <p className="small muted" data-testid="interval-note">
        Ranges are 95% intervals across the {data.prompts_matched} matched prompt{data.prompts_matched === 1 ? '' : 's'}: the trials
        of a prompt are averaged first, so repeated trials do not count as more evidence. They need at least{' '}
        {data.interval_min_prompts} prompts, are wide with few, and are left out when every prompt gives the same value. A range is
        centred on the mean of the per-prompt values, which can differ slightly from the cell average above it. Differences to a baseline are paired by prompt; “cannot tell”
        means the interval includes zero. Intervals are not corrected for comparing many candidates.
      </p>
      <label className="row small">
        Paired differences against
        <select value={data.baseline ?? ''} onChange={(e) => setBaseline(e.target.value)} aria-label="Baseline candidate">
          <option value="">no baseline</option>
          {data.candidates.map((c) => (
            <option key={c.candidate_key} value={c.candidate_key}>
              {c.label}
            </option>
          ))}
        </select>
      </label>
      <div className="table-wrap card">
        <table>
          <thead>
            <tr>
              <th>Candidate</th>
              <th className="num">Avg estimated token cost</th>
              <th className="num">Avg latency</th>
              <th className="num">Avg input / output (reasoning) tokens</th>
              <th className="num">Manual score</th>
              <th className="num">Judge score</th>
              <th className="num" title="Share of the judge's preferences, averaged per cell. A tie gives each judged answer an equal share.">
                Judge win rate
              </th>
              <th className="num">Failures</th>
            </tr>
          </thead>
          <tbody>
            {data.candidates.map((item) => (
              <tr key={item.candidate_key}>
                <td>
                  {item.label}
                  {data.baseline === item.candidate_key && (
                    <>
                      {' '}
                      <Badge>baseline</Badge>
                    </>
                  )}
                  <div className="small muted">
                    {item.covered_cells} usable cells · {item.runs} run{item.runs === 1 ? '' : 's'}
                  </div>
                </td>
                <td className="num">
                  <CostCell item={item} currency={data.currency} />
                </td>
                <td className="num">
                  {item.avg_latency_ms === null || item.avg_latency_ms === undefined ? '—' : formatMs(Math.round(item.avg_latency_ms))}
                  <div className="small muted">{cells(item.latency_cells)}</div>
                  <IntervalLine kind="latency" interval={item.latency_interval} shown={item.avg_latency_ms} />
                  <DifferenceLine kind="latency" difference={item.vs_baseline?.latency_ms} />
                </td>
                <td className="num">
                  {number(item.avg_input_tokens)} / {number(item.avg_output_tokens)}
                  {item.avg_reasoning_tokens !== null && item.avg_reasoning_tokens !== undefined ? ` (${number(item.avg_reasoning_tokens)})` : ''}
                  <div className="small muted" title={`Reasoning averaged over ${cells(item.reasoning_cells)}`}>
                    {cells(item.token_cells)}
                  </div>
                </td>
                <td className="num">
                  {number(item.avg_manual_score, 2)}
                  <div className="small muted">{cells(item.manual_scored)} scored</div>
                  <IntervalLine kind="score" interval={item.manual_interval} shown={item.avg_manual_score} />
                  <DifferenceLine kind="score" difference={item.vs_baseline?.manual} />
                </td>
                {withheld ? (
                  <>
                    <td className="num" title={WITHHELD}>
                      <span className="muted">withheld</span>
                      <div className="small muted">mixed judges</div>
                    </td>
                    <td className="num" title={WITHHELD}>
                      <span className="muted">withheld</span>
                      <div className="small muted">mixed judges</div>
                    </td>
                  </>
                ) : (
                  <>
                    <td className="num">
                      {number(item.avg_judge_score, 2)}
                      <div className="small muted">{cells(item.judge_scored)} judged</div>
                      <IntervalLine kind="score" interval={item.judge_interval} shown={item.avg_judge_score} />
                      <DifferenceLine kind="score" difference={item.vs_baseline?.judge} />
                    </td>
                    <td className="num">
                      {item.win_rate === null || item.win_rate === undefined ? '—' : `${Math.round(item.win_rate * 100)}%`}
                      <div className="small muted">
                        {cells(item.win_cells)} · {item.judge_comparisons} evaluation{item.judge_comparisons === 1 ? '' : 's'}
                      </div>
                      <IntervalLine kind="rate" interval={item.win_rate_interval} shown={item.win_rate} />
                      <DifferenceLine kind="rate" difference={item.vs_baseline?.win_rate} />
                    </td>
                  </>
                )}
                <td className="num">
                  {item.failures} of {item.attempts}
                  <div className="small muted">attempts{item.pending > 0 ? ` · ${item.pending} not finished` : ''}</div>
                  {item.failures_billed > 0 && (
                    <div
                      className="small muted"
                      title="Failed attempts whose provider reported token usage. Their cost is in the run totals, not in these averages."
                    >
                      {item.failures_billed} billed
                    </div>
                  )}
                </td>
              </tr>
            ))}
          </tbody>
        </table>
      </div>
      <details>
        <summary>Cells</summary>
        <div className="table-wrap">
          <table className="compact">
            <thead>
              <tr>
                <th>Prompt</th>
                <th>Trial</th>
                <th>Matched</th>
                <th>Missing a usable answer</th>
              </tr>
            </thead>
            <tbody>
              {data.cells.map((cell) => (
                <tr key={`${cell.content_hash}-${cell.trial_index}`}>
                  <td>{cell.prompt_title}</td>
                  <td>{cell.trial_index}</td>
                  <td>{cell.matched ? <Badge tone="ok">yes</Badge> : <Badge tone="warn">no</Badge>}</td>
                  <td className="small">{cell.missing.join(', ') || '—'}</td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      </details>
      {data.notes.map((note) => (
        <p key={note} className="small muted">
          {note}
        </p>
      ))}
    </div>
  )
}
