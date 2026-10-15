import type { CostEstimate, CostTotal } from '../api/types'
import { CATEGORY_LABELS, SOURCE_LABELS, formatTokens } from '../lib/format'
import { formatMoney } from '../lib/money'
import { Badge } from './ui'

/** One-line estimated token cost: exact when complete, a lower bound when incomplete. */
export function CostValue({ cost, currency = 'USD' }: { cost: CostEstimate | null | undefined; currency?: string }) {
  if (!cost) return <span className="muted">pending</span>
  if (cost.status === 'not_sent') {
    return (
      <span className="cost" title="The request was never sent, so nothing was billed.">
        {formatMoney('0', currency, 2)} <span className="muted small">(not sent)</span>
      </span>
    )
  }
  if (cost.status === 'complete') return <span className="cost">{formatMoney(cost.total, cost.currency)}</span>
  return (
    <span className="cost" title={cost.reasons?.join('\n')}>
      ≥ {formatMoney(cost.lower, cost.currency)}{' '}
      {cost.upper ? <span className="muted small">(≤ {formatMoney(cost.upper, cost.currency)})</span> : null}{' '}
      <Badge tone="warn">incomplete</Badge>
    </span>
  )
}

export function CostDetails({ cost }: { cost: CostEstimate | null | undefined }) {
  if (!cost || cost.status === 'not_sent') return null
  const custom = (cost.lines ?? []).some((line) => line.custom_rate)
  return (
    <details>
      <summary>Cost breakdown{custom ? ' · includes custom rates' : ''}</summary>
      <div className="table-wrap">
        <table className="compact">
          <thead>
            <tr>
              <th>Category</th>
              <th className="num">Tokens</th>
              <th className="num">Rate / 1M</th>
              <th className="num">Cost</th>
              <th>Source</th>
            </tr>
          </thead>
          <tbody>
            {(cost.lines ?? []).map((line) => (
              <tr key={line.category}>
                <td>{line.label}</td>
                <td className="num">
                  {line.tokens === null || line.tokens === undefined ? (
                    <span className="not-reported">{line.status === 'unassigned' ? 'split not reported' : 'not reported'}</span>
                  ) : (
                    formatTokens(line.tokens)
                  )}
                </td>
                <td className="num">{line.rate_per_million ? formatMoney(line.rate_per_million, cost.currency, 4) : '—'}</td>
                <td className="num">{line.cost ? formatMoney(line.cost, cost.currency) : '—'}</td>
                <td>
                  {line.status === 'folded' ? (
                    <span className="small muted">billed as uncached input</span>
                  ) : line.source ? (
                    <Badge tone={line.custom_rate ? 'custom' : 'neutral'}>{SOURCE_LABELS[line.source] ?? line.source}</Badge>
                  ) : line.status === 'unknown_rate' ? (
                    <Badge tone="warn">no rate</Badge>
                  ) : null}
                </td>
              </tr>
            ))}
          </tbody>
        </table>
      </div>
      {(cost.pools ?? []).length > 0 && (
        <p className="small muted">
          {(cost.pools ?? []).map((pool) => (
            <span key={pool.note}>
              {formatTokens(pool.tokens)} tokens: {pool.note} ({pool.categories.map((c) => CATEGORY_LABELS[c] ?? c).join(' or ')}).{' '}
            </span>
          ))}
        </p>
      )}
      {(cost.reasons ?? []).length > 0 && (
        <div className="callout warn small">
          <strong>Why the estimate is incomplete</strong>
          <ul>
            {(cost.reasons ?? []).map((reason) => (
              <li key={reason}>{reason}</li>
            ))}
          </ul>
        </div>
      )}
      {(cost.assumptions ?? []).length > 0 && (
        <p className="small muted">Assumptions: {(cost.assumptions ?? []).join('; ')}</p>
      )}
    </details>
  )
}

export function TotalBox({ title, total }: { title: string; total: CostTotal }) {
  let value: string
  let note = ''
  if (total.status === 'empty') {
    value = formatMoney('0', total.currency, 2)
    note = 'nothing yet'
  } else if (total.status === 'complete') {
    value = formatMoney(total.total, total.currency)
  } else {
    value = `≥ ${formatMoney(total.lower, total.currency)}`
    const parts: string[] = []
    if (total.status === 'pending') {
      parts.push(`${total.pending} pending`)
    } else {
      if (total.incomplete) parts.push(`${total.incomplete} of ${total.counted} incomplete`)
      if (total.other_currency) parts.push(`${total.other_currency} in another currency not included`)
      if (total.upper) parts.push(`≤ ${formatMoney(total.upper, total.currency)}`)
    }
    note = parts.join(' · ')
  }
  return (
    <div className="total-box">
      <div className="cost-label">{title}</div>
      <div className="value">{value}</div>
      <div className="small muted">
        Estimated token cost{note ? ` · ${note}` : ''}
      </div>
    </div>
  )
}
