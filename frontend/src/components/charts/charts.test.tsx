import { render, screen } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { describe, expect, it } from 'vitest'

import type { Comparison, ComparisonCandidate } from '../../api/types'
import { TokenBars } from './BarCharts'
import { CostScoreScatter } from './CostScoreScatter'

function candidate(partial: Partial<ComparisonCandidate>): ComparisonCandidate {
  return {
    candidate_key: partial.label ?? 'x',
    label: 'x',
    profile_name: 'x',
    publisher: 'OpenAI',
    api_style: 'openai_responses',
    runs: 1,
    attempts: 2,
    succeeded: 2,
    failures: 0,
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
    ...partial,
  }
}

function comparison(candidates: ComparisonCandidate[]): Comparison {
  return { run_ids: ['r'], currency: 'USD', cells_total: 2, cells_matched: 2, candidates, cells: [], notes: [] }
}

const DATA = comparison([
  candidate({ label: 'Cheap', avg_cost: '0.001', avg_judge_score: 3.5 }),
  candidate({ label: 'Balanced', avg_cost: '0.003', avg_judge_score: 4.2, publisher: 'Anthropic' }),
  candidate({ label: 'Dominated', avg_cost: '0.006', avg_judge_score: 3.8, publisher: 'DeepSeek' }),
  candidate({ label: 'Unpriced', avg_cost: null }),
])

describe('CostScoreScatter', () => {
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
