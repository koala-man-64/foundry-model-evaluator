import { useQuery } from '@tanstack/react-query'

import { get } from '../../api/client'
import type { Comparison, ComparisonCandidate } from '../../api/types'
import { Badge, Empty, ErrorMessage, Spinner } from '../../components/ui'
import { formatMs } from '../../lib/format'
import { formatMoney } from '../../lib/money'

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
      {excluded.map((note) => (
        <div key={note} className="small muted">
          {note}
        </div>
      ))}
    </span>
  )
}

export function ComparisonTable({ runIds }: { runIds: string[] }) {
  const params = runIds.map((id) => `run_id=${encodeURIComponent(id)}`).join('&')
  const comparison = useQuery({
    queryKey: ['comparison', ...runIds],
    queryFn: () => get<Comparison>(`/api/comparisons?${params}`),
    enabled: runIds.length > 0,
  })
  if (runIds.length === 0) return <Empty>Select one or more runs to compare.</Empty>
  if (comparison.isLoading) return <Spinner />
  if (comparison.error) return <ErrorMessage error={comparison.error} />
  const data = comparison.data
  if (!data) return null
  return (
    <div className="stack-sm" aria-label="Comparison">
      <p className="small" data-testid="coverage">
        Coverage: <strong>{data.cells_matched}</strong> of {data.cells_total} cells (prompt × trial) have a usable answer from every
        candidate. Each average uses only the cells where every candidate has that value, so the averages side by side cover the same
        prompts and trials; the count under each value says how many. Failures count all cells.
      </p>
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
                </td>
                <td className="num">
                  {number(item.avg_judge_score, 2)}
                  <div className="small muted">{cells(item.judge_scored)} judged</div>
                </td>
                <td className="num">
                  {item.win_rate === null || item.win_rate === undefined ? '—' : `${Math.round(item.win_rate * 100)}%`}
                  <div className="small muted">
                    {cells(item.win_cells)} · {item.judge_comparisons} evaluation{item.judge_comparisons === 1 ? '' : 's'}
                  </div>
                </td>
                <td className="num">
                  {item.failures} of {item.attempts}
                  <div className="small muted">attempts{item.pending > 0 ? ` · ${item.pending} not finished` : ''}</div>
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
