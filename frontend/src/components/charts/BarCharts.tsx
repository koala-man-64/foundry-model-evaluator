import type { ReactNode } from 'react'

import type { Comparison, ComparisonCandidate } from '../../api/types'
import { formatMs } from '../../lib/format'
import { ChartFrame, Tooltip, useTooltip } from './ChartFrame'
import { compact, linearTicks, niceDomain, scaleLinear, tokenSplit, truncate } from './chartMath'

const WIDTH = 960
const HEIGHT = 380
const MARGIN = { top: 22, right: 16, bottom: 58, left: 56 }
const PLOT = { x: MARGIN.left, y: MARGIN.top, width: WIDTH - MARGIN.left - MARGIN.right, height: HEIGHT - MARGIN.top - MARGIN.bottom }
const GAP = 2 // surface gap between stacked segments

interface Segment {
  name: string
  value: number
  color: string
}

interface Bar {
  key: string
  label: string
  total: number
  totalText: string
  segments: Segment[]
  tooltip: ReactNode
}

function BarPlot({ bars, format, axisLabel }: { bars: Bar[]; format: (v: number) => string; axisLabel: string }) {
  const { container, tooltip, markProps } = useTooltip()
  const max = Math.max(...bars.map((b) => b.total), 0)
  const domain = niceDomain(0, max || 1)
  const y = scaleLinear([0, domain[1]], [PLOT.y + PLOT.height, PLOT.y])
  const ticks = linearTicks(0, domain[1])
  const slot = PLOT.width / Math.max(bars.length, 1)
  const barWidth = Math.min(56, slot * 0.66)
  const labelLength = Math.max(8, Math.floor(slot / 6.2))
  return (
    <div className="chart" ref={container}>
      <svg viewBox={`0 0 ${WIDTH} ${HEIGHT}`}>
        {ticks.map((t) => (
          <g key={t}>
            <line className="grid" x1={PLOT.x} x2={PLOT.x + PLOT.width} y1={y(t)} y2={y(t)} />
            <text x={PLOT.x - 8} y={y(t) + 4} textAnchor="end">
              {format(t)}
            </text>
          </g>
        ))}
        <line className="axis" x1={PLOT.x} x2={PLOT.x + PLOT.width} y1={y(0)} y2={y(0)} />
        <text transform={`translate(14 ${PLOT.y + PLOT.height / 2}) rotate(-90)`} textAnchor="middle">
          {axisLabel}
        </text>
        {bars.map((bar, index) => {
          const cx = PLOT.x + slot * index + slot / 2
          let base = y(0)
          return (
            <g key={bar.key} {...markProps(`${bar.label}: ${bar.totalText}`, bar.tooltip)} data-testid="chart-bar">
              <rect x={cx - slot / 2} y={PLOT.y} width={slot} height={PLOT.height} fill="transparent" />
              {bar.segments.map((segment, s) => {
                const height = Math.max(y(0) - y(segment.value) - (s > 0 ? GAP : 0), 0)
                const top = base - height - (s > 0 ? GAP : 0)
                const last = s === bar.segments.length - 1
                base = top
                return (
                  <g key={segment.name}>
                    <rect x={cx - barWidth / 2} y={top} width={barWidth} height={height} fill={segment.color} rx={last ? 4 : 0} />
                    {last && height > 4 && <rect x={cx - barWidth / 2} y={top + height - 4} width={barWidth} height={4} fill={segment.color} />}
                    {height >= 18 && bar.segments.length > 1 && (
                      <text x={cx} y={top + height / 2 + 4} textAnchor="middle" className={s === 0 ? 'on-answer' : 'label-strong'}>
                        {format(segment.value)}
                      </text>
                    )}
                  </g>
                )
              })}
              <text x={cx} y={y(bar.total) - 6} textAnchor="middle" className="label-strong">
                {bar.totalText}
              </text>
              <text x={cx} y={PLOT.y + PLOT.height + 16} textAnchor="middle">
                <title>{bar.label}</title>
                {truncate(bar.label, labelLength)}
              </text>
            </g>
          )
        })}
      </svg>
      <Tooltip state={tooltip} />
    </div>
  )
}

function cells(count: number): string {
  return `${count} cell${count === 1 ? '' : 's'}`
}

/** Average output tokens per task, split into the answer and the reasoning the provider reported. */
export function TokenBars({ comparison }: { comparison: Comparison }) {
  const rows = comparison.candidates
    .filter((c) => c.avg_output_tokens !== null && c.avg_output_tokens !== undefined)
    .sort((a, b) => (a.avg_output_tokens ?? 0) - (b.avg_output_tokens ?? 0))
  const missing = comparison.candidates.filter((c) => c.avg_output_tokens === null || c.avg_output_tokens === undefined)
  const bars: Bar[] = rows.map((c) => {
    const output = c.avg_output_tokens ?? 0
    const split = tokenSplit(output, c.avg_reasoning_tokens)
    const segments: Segment[] = [{ name: 'answer', value: split.answer, color: 'var(--chart-answer)' }]
    if (split.reasoning !== null && split.reasoning > 0) segments.push({ name: 'reasoning', value: split.reasoning, color: 'var(--chart-reasoning)' })
    return {
      key: c.candidate_key,
      label: c.label,
      total: output,
      totalText: compact(output),
      segments,
      tooltip: (
        <>
          <strong>{c.label}</strong>
          <div>Output: {Math.round(output).toLocaleString('en-US')} tokens ({cells(c.token_cells)})</div>
          <div>Answer: {Math.round(split.answer).toLocaleString('en-US')}</div>
          <div>Reasoning: {split.reasoning === null ? 'not reported' : Math.round(split.reasoning).toLocaleString('en-US')}</div>
          <div>Input: {c.avg_input_tokens === null || c.avg_input_tokens === undefined ? 'not reported' : Math.round(c.avg_input_tokens).toLocaleString('en-US')}</div>
        </>
      ),
    }
  })
  const noReasoning = rows.filter((c) => c.avg_reasoning_tokens === null || c.avg_reasoning_tokens === undefined)
  return (
    <ChartFrame
      title="Output tokens per task"
      subtitle="Average output tokens per prompt and trial, over the cells every candidate shares. Reasoning is part of the output and billed once."
      summary={`Average output tokens per task for ${bars.length} candidates, from ${bars[0]?.label ?? '—'} to ${bars.at(-1)?.label ?? '—'}.`}
      legend={
        <div className="chart-legend">
          <span>
            <span className="swatch" style={{ background: 'var(--chart-answer)' }} />
            Answer
          </span>
          <span>
            <span className="swatch" style={{ background: 'var(--chart-reasoning)' }} />
            Reasoning
          </span>
        </div>
      }
      footer={
        (noReasoning.length > 0 || missing.length > 0) && (
          <p className="small muted" data-testid="token-notes">
            {noReasoning.length > 0 && `Reasoning not reported for ${noReasoning.map((c) => c.label).join(', ')}. `}
            {missing.length > 0 && `Not plotted (output not reported): ${missing.map((c) => c.label).join(', ')}.`}
          </p>
        )
      }
      rows={rows}
      rowKey={(c) => c.candidate_key}
      columns={[
        { header: 'Candidate', value: (c) => c.label },
        { header: 'Output', value: (c) => Math.round(c.avg_output_tokens ?? 0).toLocaleString('en-US'), numeric: true },
        { header: 'Reasoning', value: (c) => (c.avg_reasoning_tokens === null || c.avg_reasoning_tokens === undefined ? 'not reported' : Math.round(c.avg_reasoning_tokens).toLocaleString('en-US')), numeric: true },
        { header: 'Input', value: (c) => (c.avg_input_tokens === null || c.avg_input_tokens === undefined ? 'not reported' : Math.round(c.avg_input_tokens).toLocaleString('en-US')), numeric: true },
        { header: 'Cells', value: (c) => c.token_cells, numeric: true },
      ]}
    >
      {bars.length ? <BarPlot bars={bars} format={compact} axisLabel="Output tokens" /> : <p className="muted small">No output token counts to show.</p>}
    </ChartFrame>
  )
}

/** Average latency per task. */
export function LatencyBars({ comparison }: { comparison: Comparison }) {
  const rows: ComparisonCandidate[] = comparison.candidates
    .filter((c) => c.avg_latency_ms !== null && c.avg_latency_ms !== undefined)
    .sort((a, b) => (a.avg_latency_ms ?? 0) - (b.avg_latency_ms ?? 0))
  const bars: Bar[] = rows.map((c) => {
    const ms = c.avg_latency_ms ?? 0
    return {
      key: c.candidate_key,
      label: c.label,
      total: ms,
      totalText: formatMs(Math.round(ms)),
      segments: [{ name: 'latency', value: ms, color: 'var(--chart-1)' }],
      tooltip: (
        <>
          <strong>{c.label}</strong>
          <div>
            Average latency: {formatMs(Math.round(ms))} ({cells(c.latency_cells)})
          </div>
        </>
      ),
    }
  })
  return (
    <ChartFrame
      title="Latency per task"
      subtitle="Average time from sending the request to the complete answer, over the cells every candidate shares."
      summary={`Average latency for ${bars.length} candidates.`}
      rows={rows}
      rowKey={(c) => c.candidate_key}
      columns={[
        { header: 'Candidate', value: (c) => c.label },
        { header: 'Average latency', value: (c) => formatMs(Math.round(c.avg_latency_ms ?? 0)), numeric: true },
        { header: 'Cells', value: (c) => c.latency_cells, numeric: true },
      ]}
    >
      {bars.length ? (
        <BarPlot bars={bars} format={(v) => formatMs(Math.round(v))} axisLabel="Latency" />
      ) : (
        <p className="muted small">No latency to show.</p>
      )}
    </ChartFrame>
  )
}
