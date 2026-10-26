import { useMemo, useState } from 'react'

import type { Comparison, ComparisonCandidate } from '../../api/types'
import { formatMoney } from '../../lib/money'
import { ChartFrame, Tooltip, useTooltip } from './ChartFrame'
import {
  linearTicks,
  logTicks,
  median,
  moneyTick,
  niceDomain,
  paretoFrontier,
  placeLabels,
  publisherSlot,
  scaleLinear,
  scaleLog,
} from './chartMath'

type ScoreKind = 'judge' | 'manual' | 'checks'

const WIDTH = 960
const HEIGHT = 420
const MARGIN = { top: 16, right: 24, bottom: 44, left: 52 }
const PLOT = { x: MARGIN.left, y: MARGIN.top, width: WIDTH - MARGIN.left - MARGIN.right, height: HEIGHT - MARGIN.top - MARGIN.bottom }
const LABEL_ALL_UP_TO = 12

interface Datum {
  key: string
  x: number
  y: number
  candidate: ComparisonCandidate
  cost: string
  cells: number
}

export function publisherColor(publisher: string): string {
  const slot = publisherSlot(publisher)
  return slot ? `var(--chart-${slot})` : 'var(--chart-other)'
}

function score(candidate: ComparisonCandidate, kind: ScoreKind): number | null {
  if (kind === 'checks') {
    const passed = candidate.avg_checks_passed
    return passed === null || passed === undefined ? null : passed * 100
  }
  const value = kind === 'judge' ? candidate.avg_judge_score : candidate.avg_manual_score
  return value === null || value === undefined ? null : value
}

/** Quality against estimated token cost per task, with the Pareto frontier and the most attractive quadrant. */
export function CostScoreScatter({ comparison }: { comparison: Comparison }) {
  const hasJudge = comparison.candidates.some((c) => c.cost_quality?.judge || (c.avg_judge_score !== null && c.avg_judge_score !== undefined))
  const [chosen, setKind] = useState<ScoreKind>(hasJudge ? 'judge' : 'manual')
  // A "checks" choice from an earlier comparison does not apply when these runs have no checks.
  const kind: ScoreKind = chosen === 'checks' && !comparison.has_checks ? (hasJudge ? 'judge' : 'manual') : chosen
  const { container, tooltip, markProps } = useTooltip()
  const currency = comparison.currency

  const judgeGap = comparison.judge_bases > 1 ? 'judge scores use different rubrics or judges' : 'not judged'
  const { data, unplotted } = useMemo(() => {
    const plotted: Datum[] = []
    const missing: { label: string; reason: string }[] = []
    for (const candidate of comparison.candidates) {
      const point = candidate.cost_quality?.[kind]
      if (point) {
        if (Number(point.avg_cost) <= 0) missing.push({ label: candidate.label, reason: `cost ${formatMoney('0', currency)}` })
        else plotted.push({
          key: candidate.candidate_key,
          x: Number(point.avg_cost),
          y: kind === 'checks' ? point.avg_score * 100 : point.avg_score,
          cost: point.avg_cost,
          cells: point.cells,
          candidate,
        })
      } else if (candidate.avg_cost === null) missing.push({ label: candidate.label, reason: 'no complete cost estimate' })
      else if (score(candidate, kind) === null)
        missing.push({
          label: candidate.label,
          reason: kind === 'judge' ? judgeGap : kind === 'checks' ? 'no checked answers' : 'not scored by hand',
        })
      else missing.push({ label: candidate.label, reason: 'no comparable shared cells with both cost and quality' })
    }
    return { data: plotted, unplotted: missing }
  }, [comparison.candidates, kind, judgeGap, currency])

  const frontier = useMemo(() => paretoFrontier(data), [data])
  const frontierKeys = new Set(frontier.map((d) => d.key))
  const publishers = [...new Set(data.map((d) => (publisherSlot(d.candidate.publisher) ? d.candidate.publisher : 'Other')))]

  const xs = data.map((d) => d.x)
  const ys = data.map((d) => d.y)
  const xDomain: [number, number] = xs.length ? [Math.min(...xs) / 1.6, Math.max(...xs) * 1.6] : [0.0001, 1]
  const padded: [number, number] = ys.length
    ? niceDomain(Math.min(...ys) - (kind === 'checks' ? 5 : 0.25), Math.max(...ys) + (kind === 'checks' ? 5 : 0.25))
    : [1, 5]
  // Pass rates are percentages: the axis never runs below 0% or above 100%.
  const yDomain: [number, number] = kind === 'checks' ? [Math.max(0, padded[0]), Math.min(100, padded[1])] : padded
  const x = scaleLog(xDomain, [PLOT.x, PLOT.x + PLOT.width])
  const y = scaleLinear(yDomain, [PLOT.y + PLOT.height, PLOT.y])
  const xTicks = logTicks(xDomain[0], xDomain[1])
  const yTicks = linearTicks(yDomain[0], yDomain[1])
  const medX = data.length >= 3 ? median(xs) : null
  const medY = data.length >= 3 ? median(ys) : null

  const labelled = data.length <= LABEL_ALL_UP_TO ? data : frontier
  const labels = placeLabels(
    [...labelled]
      .sort((a, b) => Number(frontierKeys.has(b.key)) - Number(frontierKeys.has(a.key)))
      .map((d) => ({ key: d.key, text: d.candidate.label, x: x(d.x), y: y(d.y) })),
    PLOT,
  )
  const kindLabel =
    kind === 'judge' ? 'Judge score (weighted overall)' : kind === 'checks' ? 'Checks passed (%)' : 'Manual score'
  const summary = `${kindLabel} against average estimated token cost per task for ${data.length} candidates; ${frontier.length} on the Pareto frontier.`

  return (
    <ChartFrame
      title="Quality vs. estimated token cost per task"
      subtitle={`${kindLabel} · both axes use the same prompt/trial cells shared by candidates with cost and this score`}
      summary={summary}
      controls={
        <label className="row small">
          Score
          <select value={kind} onChange={(e) => setKind(e.target.value as ScoreKind)} aria-label="Score">
            <option value="judge">Judge</option>
            <option value="manual">Manual</option>
            {comparison.has_checks && <option value="checks">Checks passed</option>}
          </select>
        </label>
      }
      legend={
        <div className="chart-legend" data-testid="scatter-legend">
          {medX !== null && (
            <span>
              <span className="swatch quadrant" />
              Most attractive quadrant
            </span>
          )}
          {frontier.length > 1 && (
            <span>
              <span className="swatch line" />
              Pareto frontier
            </span>
          )}
          {publishers.map((p) => (
            <span key={p}>
              <span className="swatch dot" style={{ background: publisherColor(p) }} />
              {p}
            </span>
          ))}
        </div>
      }
      footer={
        unplotted.length > 0 && (
          <p className="small muted" data-testid="scatter-unplotted">
            Not plotted: {unplotted.map((u) => `${u.label} (${u.reason})`).join('; ')}.
          </p>
        )
      }
      rows={data}
      rowKey={(d) => d.key}
      columns={[
        { header: 'Candidate', value: (d) => d.candidate.label },
        { header: 'Publisher', value: (d) => d.candidate.publisher },
        { header: `Avg cost (${currency})`, value: (d) => formatMoney(d.cost, currency), numeric: true },
        { header: kindLabel, value: (d) => d.y.toFixed(2), numeric: true },
        { header: 'Shared cells', value: (d) => String(d.cells), numeric: true },
        { header: 'Pareto', value: (d) => (frontierKeys.has(d.key) ? 'yes' : '') },
      ]}
    >
      <div className="chart" ref={container} data-testid="cost-score-chart">
        {data.length === 0 ? (
          <p className="muted small">Nothing to plot yet: candidates need a complete cost estimate and a {kind} score.</p>
        ) : (
          <div className="chart-scroll">
            <svg viewBox={`0 0 ${WIDTH} ${HEIGHT}`} aria-hidden={false}>
              {medX !== null && medY !== null && (
                <rect
                  x={PLOT.x}
                  y={PLOT.y}
                  width={x(medX) - PLOT.x}
                  height={y(medY) - PLOT.y}
                  fill="var(--chart-quadrant)"
                  data-testid="attractive-quadrant"
                />
              )}
              {yTicks.map((t) => (
                <g key={`y${t}`}>
                  <line className="grid" x1={PLOT.x} x2={PLOT.x + PLOT.width} y1={y(t)} y2={y(t)} />
                  <text x={PLOT.x - 8} y={y(t) + 4} textAnchor="end">
                    {t}
                  </text>
                </g>
              ))}
              {xTicks.map((t) => (
                <g key={`x${t}`}>
                  <line className="grid" x1={x(t)} x2={x(t)} y1={PLOT.y} y2={PLOT.y + PLOT.height} />
                  <text x={x(t)} y={PLOT.y + PLOT.height + 16} textAnchor="middle">
                    {moneyTick(t, currency)}
                  </text>
                </g>
              ))}
              <line className="axis" x1={PLOT.x} x2={PLOT.x + PLOT.width} y1={PLOT.y + PLOT.height} y2={PLOT.y + PLOT.height} />
              <text x={PLOT.x + PLOT.width / 2} y={HEIGHT - 6} textAnchor="middle">
                Average estimated token cost per task (log scale)
              </text>
              <text transform={`translate(14 ${PLOT.y + PLOT.height / 2}) rotate(-90)`} textAnchor="middle">
                {kindLabel}
              </text>
              {frontier.length > 1 && (
                <polyline
                  points={frontier.map((d) => `${x(d.x)},${y(d.y)}`).join(' ')}
                  fill="none"
                  stroke="var(--text-muted)"
                  strokeWidth={2}
                  strokeDasharray="2 4"
                  strokeLinecap="round"
                  data-testid="pareto-line"
                />
              )}
              {data.map((d) => (
                <circle
                  key={d.key}
                  cx={x(d.x)}
                  cy={y(d.y)}
                  r={6}
                  fill={publisherColor(d.candidate.publisher)}
                  stroke="var(--surface)"
                  strokeWidth={2}
                  {...markProps(
                    `${d.candidate.label}: ${formatMoney(d.cost, currency)}, score ${d.y.toFixed(2)}`,
                    <>
                      <strong>{d.candidate.label}</strong>
                      <div>{d.candidate.publisher}</div>
                      <div>Avg estimated token cost: {formatMoney(d.cost, currency)}</div>
                      <div>{kindLabel}: {d.y.toFixed(2)}</div>
                      <div>{d.cells} shared prompt/trial cell{d.cells === 1 ? '' : 's'} with both cost and quality</div>
                      {frontierKeys.has(d.key) && <div>On the Pareto frontier</div>}
                    </>,
                  )}
                />
              ))}
              {labels.map((l) => (
                <text
                  key={l.key}
                  x={l.lx}
                  y={l.ly}
                  textAnchor={l.anchor}
                  className={frontierKeys.has(l.key) ? 'label-strong halo' : 'halo'}
                  pointerEvents="none"
                >
                  {l.text}
                </text>
              ))}
            </svg>
          </div>
        )}
        <Tooltip state={tooltip} />
      </div>
    </ChartFrame>
  )
}
