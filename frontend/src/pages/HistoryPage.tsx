import { useState } from 'react'
import { Link } from 'react-router'

import { useRuns } from '../api/hooks'
import type { CostTotal } from '../api/types'
import { Badge, Empty, ErrorMessage, Spinner, StatusBadge } from '../components/ui'
import { formatDate } from '../lib/format'
import { formatMoney } from '../lib/money'

export function TotalCell({ total }: { total: CostTotal }) {
  if (total.status === 'empty') return <span className="muted">—</span>
  if (total.status === 'complete') return <span>{formatMoney(total.total, total.currency)}</span>
  return (
    <span title={total.status === 'pending' ? 'Some calls are still running' : `${total.incomplete} incomplete estimates`}>
      ≥ {formatMoney(total.lower, total.currency)} <Badge tone="warn">{total.status === 'pending' ? 'pending' : 'incomplete'}</Badge>
    </span>
  )
}

export function HistoryPage() {
  const [kind, setKind] = useState('')
  const runs = useRuns(kind ? `?kind=${kind}` : '')
  return (
    <div className="stack">
      <div className="row">
        <label className="row">
          Show
          <select value={kind} onChange={(e) => setKind(e.target.value)}>
            <option value="">All runs</option>
            <option value="compare">Comparisons</option>
            <option value="suite">Suite runs</option>
            <option value="smoke">Smoke tests</option>
          </select>
        </label>
      </div>
      {runs.isLoading && <Spinner />}
      <ErrorMessage error={runs.error} />
      {runs.data && runs.data.length === 0 && <Empty>No runs yet. Start one from Compare.</Empty>}
      {runs.data && runs.data.length > 0 && (
        <div className="card table-wrap">
          <table>
            <thead>
              <tr>
                <th>Created</th>
                <th>Run</th>
                <th>Candidates</th>
                <th>Status</th>
                <th className="num">Calls</th>
                <th className="num">Generation</th>
                <th className="num">Judging</th>
                <th className="num">Combined (estimated token cost)</th>
              </tr>
            </thead>
            <tbody>
              {runs.data.map((summary) => {
                const total = Object.values(summary.counts).reduce((a, b) => a + b, 0)
                const failed = (summary.counts.failed ?? 0) + (summary.counts.interrupted ?? 0)
                return (
                  <tr key={summary.run.id}>
                    <td className="nowrap">{formatDate(summary.run.created_at)}</td>
                    <td>
                      <Link to={`/runs/${summary.run.id}`}>{summary.run.title || 'Untitled run'}</Link>
                      <div className="small muted">{summary.run.kind}</div>
                    </td>
                    <td className="small">{summary.candidate_labels.join(', ')}</td>
                    <td>
                      <StatusBadge status={summary.run.status} />
                    </td>
                    <td className="num">
                      {summary.counts.succeeded ?? 0}/{total}
                      {failed ? <div className="small muted">{failed} failed</div> : null}
                    </td>
                    <td className="num">
                      <TotalCell total={summary.generation_cost} />
                    </td>
                    <td className="num">
                      <TotalCell total={summary.judging_cost} />
                    </td>
                    <td className="num">
                      <TotalCell total={summary.combined_cost} />
                    </td>
                  </tr>
                )
              })}
            </tbody>
          </table>
        </div>
      )}
    </div>
  )
}
