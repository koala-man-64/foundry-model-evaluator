import { render, screen } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { describe, expect, it } from 'vitest'

import type { Comparison, ComparisonCandidate } from '../../api/types'
import { TokenBars } from './BarCharts'
import { CostScoreScatter } from './CostScoreScatter'

function candidate(partial: Partial<ComparisonCandidate>): ComparisonCandidate {
  const result: ComparisonCandidate = {
    candidate_key: partial.label ?? 'x',
    label: 'x',
    profile_name: 'x',
    publisher: 'OpenAI',
    api_style: 'openai_responses',
    runs: 1,
    attempts: 2,
    succeeded: 2,
    failures: 0,
    failures_billed: 0,
    pending: 0,
    covered_cells: 2,
    avg_cost: '0.002',
    cost_cells: 2,
    cost_excluded: 0,
    cost_other_currency: 0,
    avg_latency_ms: 900,
    latency_cells: 2,
    avg_input_tokens: 100,
    avg_output_tokens: 1000,
    token_cells: 2,
    avg_reasoning_tokens: 400,
    reasoning_cells: 2,
    avg_manual_score: null,
    manual_scored: 0,
    avg_judge_score: 4,
    judge_scored: 2,
    judge_wins: 1,
    judge_comparisons: 2,
    win_cells: 2,
    win_rate: 0.5,
    custom_rates: false,
    checks_cells: 0,
    judge_evaluations: 2,
    judge_spread_cells: 0,
    ...partial,
  }
  const point = (score: number | null | undefined) => result.avg_cost && score !== null && score !== undefined
    ? { avg_cost: result.avg_cost, avg_score: score, cells: 2, custom_rates: false }
    : null
  return {
    ...result,
    cost_quality: partial.cost_quality ?? {
      judge: point(result.avg_judge_score),
      manual: point(result.avg_manual_score),
      checks: point(result.avg_checks_passed),
    },
  }
}

function comparison(candidates: ComparisonCandidate[], judgeBases = 1): Comparison {
  return {
    run_ids: ['r'],
    currency: 'USD',
    cells_total: 2,
    cells_matched: 2,
    judge_bases: judgeBases,
    prompts_matched: 1,
    interval_min_prompts: 3,
    baseline: null,
    has_checks: false,
    checks_not_checked: 0,
    checks_unknown: 0,
    candidates,
    cells: [],
    notes: [],
  }
}

const DATA = comparison([
  candidate({ label: 'Cheap', avg_cost: '0.001', avg_judge_score: 3.5 }),
  candidate({ label: 'Balanced', avg_cost: '0.003', avg_judge_score: 4.2, publisher: 'Anthropic' }),
  candidate({ label: 'Dominated', avg_cost: '0.006', avg_judge_score: 3.8, publisher: 'DeepSeek' }),
  candidate({ label: 'Unpriced', avg_cost: null }),
])

describe('CostScoreScatter', () => {
  it('uses paired coordinates for points, the Pareto frontier, hover details and the table', async () => {
    const paired = comparison([
      candidate({
        label: 'Misleading aggregate', avg_cost: '0.001', avg_judge_score: 5,
        cost_quality: { judge: { avg_cost: '0.004', avg_score: 2, cells: 1, custom_rates: false } },
      }),
      candidate({
        label: 'Better on shared tasks', avg_cost: '0.003', avg_judge_score: 4,
        cost_quality: { judge: { avg_cost: '0.003', avg_score: 4, cells: 1, custom_rates: false } },
      }),
    ])
    render(<CostScoreScatter comparison={paired} />)
    const point = screen.getByRole('img', { name: 'Misleading aggregate: $0.004000, score 2.00' })
    await userEvent.hover(point)
    expect(screen.getByRole('tooltip')).toHaveTextContent('1 shared prompt/trial cell')
    expect(screen.getByRole('tooltip')).not.toHaveTextContent('On the Pareto frontier')
    await userEvent.click(screen.getByRole('button', { name: 'Show table' }))
    expect(screen.getByRole('row', { name: /Misleading aggregate/ })).toHaveTextContent('$0.004')
    expect(screen.getByRole('row', { name: /Misleading aggregate/ })).not.toHaveTextContent('yes')
    expect(screen.getByRole('row', { name: /Better on shared tasks/ })).toHaveTextContent('yes')
    expect(screen.getByRole('columnheader', { name: 'Shared cells' })).toBeInTheDocument()
  })

  it('withholds points when cost and quality have no common cells even when their counts match', () => {
    render(<CostScoreScatter comparison={comparison([
      candidate({ label: 'Disjoint', cost_cells: 1, judge_scored: 1, cost_quality: { judge: null } }),
    ])} />)
    expect(screen.queryByRole('img', { name: /^Disjoint/ })).not.toBeInTheDocument()
    expect(screen.getByTestId('scatter-unplotted')).toHaveTextContent('no comparable shared cells with both cost and quality')
  })

  it('plots valid paired judge evidence even when standalone metric intersections are empty', () => {
    render(<CostScoreScatter comparison={comparison([
      candidate({
        label: 'Paired only', avg_cost: null, avg_judge_score: null,
        cost_quality: { judge: { avg_cost: '0.004', avg_score: 4, cells: 1, custom_rates: false } },
      }),
    ])} />)
    expect(screen.getByRole('combobox', { name: 'Score' })).toHaveValue('judge')
    expect(screen.getByRole('img', { name: 'Paired only: $0.004000, score 4.00' })).toBeInTheDocument()
  })

  it.each(['manual', 'checks'] as const)('uses paired %s scores when the score source changes', async (kind) => {
    const paired = comparison([
      candidate({
        label: 'Paired', avg_manual_score: 5, manual_scored: 2, avg_checks_passed: 1, checks_cells: 2,
        cost_quality: { [kind]: { avg_cost: '0.009', avg_score: kind === 'checks' ? 0.75 : 3, cells: 1, custom_rates: false } },
      }),
    ])
    paired.has_checks = true
    render(<CostScoreScatter comparison={paired} />)
    await userEvent.selectOptions(screen.getByRole('combobox', { name: 'Score' }), kind)
    expect(screen.getByRole('img', { name: `Paired: $0.009000, score ${kind === 'checks' ? '75.00' : '3.00'}` })).toBeInTheDocument()
  })

  it('plots priced and scored candidates, with the frontier, the quadrant and a legend', () => {
    render(<CostScoreScatter comparison={DATA} />)
    expect(screen.getByTestId('pareto-line')).toBeInTheDocument()
    expect(screen.getByTestId('attractive-quadrant')).toBeInTheDocument()
    expect(screen.getAllByRole('img', { name: /: \$/ })).toHaveLength(3)
    const legend = screen.getByTestId('scatter-legend')
    for (const name of ['Pareto frontier', 'Most attractive quadrant', 'OpenAI', 'Anthropic', 'Other']) {
      expect(legend).toHaveTextContent(name)
    }
    expect(screen.getByTestId('scatter-unplotted')).toHaveTextContent('Unpriced (no complete cost estimate)')
  })

  it('has a table view that marks the frontier', async () => {
    render(<CostScoreScatter comparison={DATA} />)
    await userEvent.click(screen.getByRole('button', { name: 'Show table' }))
    const table = screen.getByRole('table')
    expect(table).toHaveTextContent('Balanced')
    expect(screen.getAllByRole('row').filter((row) => row.textContent?.includes('yes'))).toHaveLength(2)
  })

  it('says why judge scores are missing when they mix rubrics or judges', async () => {
    const mixed = comparison(
      [candidate({ label: 'A', avg_judge_score: null, judge_scored: 0, avg_manual_score: 4, manual_scored: 2 })],
      2,
    )
    render(<CostScoreScatter comparison={mixed} />)
    await userEvent.selectOptions(screen.getByRole('combobox', { name: 'Score' }), 'judge')
    expect(screen.getByTestId('scatter-unplotted')).toHaveTextContent('A (judge scores use different rubrics or judges)')
  })

  it('shows the details of a point on hover', async () => {
    render(<CostScoreScatter comparison={DATA} />)
    await userEvent.hover(screen.getByRole('img', { name: /^Balanced/ }))
    expect(screen.getByRole('tooltip')).toHaveTextContent('On the Pareto frontier')
  })
})

describe('TokenBars', () => {
  it('stacks answer and reasoning and says when reasoning is not reported', () => {
    render(
      <TokenBars
        comparison={comparison([
          candidate({ label: 'Reasoner', avg_output_tokens: 3000, avg_reasoning_tokens: 2000 }),
          candidate({ label: 'Plain', avg_output_tokens: 800, avg_reasoning_tokens: null }),
        ])}
      />,
    )
    const bars = screen.getAllByTestId('chart-bar')
    expect(bars.map((b) => b.getAttribute('aria-label'))).toEqual(['Plain: 800', 'Reasoner: 3k'])
    expect(screen.getByTestId('token-notes')).toHaveTextContent('Reasoning not reported for Plain')
  })
})
