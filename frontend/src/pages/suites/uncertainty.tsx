import type { Difference, Interval, MoneyDifference, MoneyInterval } from '../../api/types'
import { Badge } from '../../components/ui'
import { formatMs } from '../../lib/format'
import { formatMoney } from '../../lib/money'

/** What a metric measures, which decides formatting and which direction is better. */
export type MetricKind = 'cost' | 'latency' | 'score' | 'rate'

function value(kind: MetricKind, v: number | string, currency: string): string {
  if (kind === 'cost') return formatMoney(String(v), currency)
  const n = Number(v)
  if (kind === 'latency') return formatMs(Math.round(n))
  if (kind === 'rate') return `${Math.round(n * 100)}%`
  return n.toFixed(2)
}

function signed(kind: MetricKind, v: number | string, currency: string): string {
  const n = Number(v)
  const sign = n > 0 ? '+' : n < 0 ? '−' : '±'
  const magnitude = kind === 'cost' ? String(v).replace(/^-/, '') : Math.abs(n)
  if (kind === 'rate') return `${sign}${Math.round(Math.abs(n) * 100)} pts`
  return `${sign}${value(kind, magnitude, currency)}`
}

/** The 95% range across prompts under an average, or nothing when there are too few prompts. */
export function IntervalLine({
  kind,
  interval,
  shown,
  currency = 'USD',
}: {
  kind: MetricKind
  interval: Interval | MoneyInterval | null | undefined
  /** The average displayed above; when the per-prompt mean rounds differently, the line says so. */
  shown?: number | string | null
  currency?: string
}) {
  if (!interval) return null
  const centre = value(kind, interval.mean, currency)
  const differs = shown !== null && shown !== undefined && value(kind, shown, currency) !== centre
  return (
    <div className="small muted" data-testid="interval" title={`95% interval across ${interval.prompts} prompts`}>
      95% {value(kind, interval.low, currency)}–{value(kind, interval.high, currency)}
      {differs && ` around the per-prompt mean ${centre}`}
    </div>
  )
}

const VERDICTS: Record<MetricKind, Record<string, string>> = {
  cost: { higher: 'costs more', lower: 'cheaper' },
  latency: { higher: 'slower', lower: 'faster' },
  score: { higher: 'higher', lower: 'lower' },
  rate: { higher: 'wins more', lower: 'wins less' },
}

/** A paired difference to the baseline with its verdict; better and worse are coloured. */
export function DifferenceLine({
  kind,
  difference,
  currency = 'USD',
}: {
  kind: MetricKind
  difference: Difference | MoneyDifference | null | undefined
  currency?: string
}) {
  if (!difference) return null
  const { verdict } = difference
  const better = kind === 'cost' || kind === 'latency' ? 'lower' : 'higher'
  const label =
    verdict === 'cannot_tell' ? 'cannot tell' : verdict === 'insufficient_data' ? 'too few prompts' : VERDICTS[kind][verdict]
  const tone = verdict === better ? 'ok' : verdict === 'higher' || verdict === 'lower' ? 'warn' : undefined
  const range =
    difference.low !== null && difference.low !== undefined && difference.high !== null && difference.high !== undefined
      ? ` (${signed(kind, difference.low, currency)} to ${signed(kind, difference.high, currency)})`
      : ''
  return (
    <div className="small" data-testid="difference" title={`Paired over ${difference.prompts} prompts`}>
      vs baseline {signed(kind, difference.mean, currency)}
      <span className="muted">{range}</span> <Badge tone={tone}>{label}</Badge>
    </div>
  )
}
