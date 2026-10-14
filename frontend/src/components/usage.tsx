import type { Attempt, CostEstimate, UsageFacts } from '../api/types'
import { formatMs, formatTokens } from '../lib/format'

function Value({ value, suffix }: { value: number | null | undefined; suffix?: string }) {
  if (value === null || value === undefined) return <span className="not-reported">not reported</span>
  return (
    <>
      {formatTokens(value)}
      {suffix ? <span className="muted small"> {suffix}</span> : null}
    </>
  )
}

function uncachedFromCost(cost: CostEstimate | null | undefined): number | null {
  const line = cost?.lines?.find((l) => l.category === 'input_uncached')
  return line && line.tokens !== null && line.tokens !== undefined ? line.tokens : null
}

/** Token usage as reported by the provider; missing details are shown as "not reported". */
export function UsageTable({ usage, cost }: { usage: UsageFacts | null | undefined; cost?: CostEstimate | null }) {
  if (!usage) return <p className="not-reported small">Usage not reported.</p>
  const derived = usage.input_uncached === null || usage.input_uncached === undefined ? uncachedFromCost(cost) : null
  return (
    <dl className="metric-grid">
      <dt>Total input</dt>
      <dd>
        <Value value={usage.input_total} />
      </dd>
      <dt>Uncached input</dt>
      <dd>
        {usage.input_uncached !== null && usage.input_uncached !== undefined ? (
          <Value value={usage.input_uncached} suffix={usage.input_uncached_derived ? '(derived)' : undefined} />
        ) : derived !== null ? (
          <Value value={derived} suffix="(derived)" />
        ) : (
          <span className="not-reported">not reported</span>
        )}
      </dd>
      <dt>Cache read</dt>
      <dd>
        <Value value={usage.cache_read} />
      </dd>
      <dt>Cache write</dt>
      <dd>
        <Value value={usage.cache_write} />
        {usage.cache_write_5m !== null && usage.cache_write_5m !== undefined && (
          <span className="muted small">
            {' '}
            (5m {formatTokens(usage.cache_write_5m)} / 1h {formatTokens(usage.cache_write_1h)})
          </span>
        )}
      </dd>
      <dt>Output</dt>
      <dd>
        <Value value={usage.output_total} />
      </dd>
      <dt>Reasoning (in output)</dt>
      <dd>
        <Value value={usage.reasoning} />
      </dd>
    </dl>
  )
}

export function TimingTable({ attempt }: { attempt: Attempt }) {
  return (
    <dl className="metric-grid">
      <dt>Total latency</dt>
      <dd>{formatMs(attempt.latency_ms)}</dd>
      <dt>First token</dt>
      <dd>{attempt.streamed ? formatMs(attempt.ttft_ms) : <span className="muted small">not streamed</span>}</dd>
    </dl>
  )
}
