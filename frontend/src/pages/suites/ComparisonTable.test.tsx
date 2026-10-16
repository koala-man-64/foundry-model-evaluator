import { render, screen, within } from '@testing-library/react'
import { describe, expect, it, vi } from 'vitest'

import type { Comparison, ComparisonCandidate } from '../../api/types'
import { ComparisonTable } from './ComparisonTable'

const state: { data: Comparison | undefined } = { data: undefined }

vi.mock('../../api/hooks', () => ({
  useComparison: () => ({ data: state.data, isLoading: false, error: null }),
}))

function candidate(label: string, partial: Partial<ComparisonCandidate> = {}): ComparisonCandidate {
  return {
    candidate_key: label,
    label,
    profile_name: label,
    publisher: 'OpenAI',
    api_style: 'openai_responses',
    runs: 2,
    attempts: 2,
    succeeded: 2,
    failures: 0,
    failures_billed: 0,
    pending: 0,
    covered_cells: 1,
    avg_cost: '0.002',
    cost_cells: 1,
    cost_excluded: 0,
    cost_other_currency: 0,
    avg_latency_ms: 900,
    latency_cells: 1,
    avg_input_tokens: 100,
    avg_output_tokens: 200,
    token_cells: 1,
    avg_reasoning_tokens: null,
    reasoning_cells: 0,
    avg_manual_score: null,
    manual_scored: 0,
    avg_judge_score: null,
    judge_scored: 0,
    judge_wins: 0,
    judge_comparisons: 0,
    win_cells: 0,
    win_rate: null,
    custom_rates: false,
    ...partial,
  }
}

function comparison(judgeBases: number, candidates: ComparisonCandidate[]): Comparison {
  return {
    run_ids: ['r1', 'r2'],
    currency: 'USD',
    cells_total: 1,
    cells_matched: 1,
    judge_bases: judgeBases,
    prompts_matched: 1,
    interval_min_prompts: 3,
    baseline: null,
    candidates,
    cells: [],
    notes: [],
  }
}

describe('ComparisonTable', () => {
  it('marks judge scores as withheld, not as unjudged, when the judge evidence is mixed', () => {
    state.data = comparison(2, [candidate('A'), candidate('B')])
    render(<ComparisonTable runIds={['r1', 'r2']} charts={false} />)
    const row = screen.getByRole('row', { name: /^A/ })
    expect(within(row).getAllByText('withheld')).toHaveLength(2)
    expect(within(row).queryByText(/judged/)).not.toBeInTheDocument()
  })

  it('shows 95% ranges and the paired difference to the baseline with a verdict', () => {
    state.data = {
      ...comparison(1, [
        candidate('Base'),
        candidate('Better', {
          avg_manual_score: 4.75,
          manual_scored: 4,
          manual_interval: { low: 4.2, high: 5, prompts: 4 },
          cost_interval: { low: '0.0015', high: '0.0025', prompts: 4 },
          vs_baseline: {
            cost: { mean: '0.0005', low: '-0.0001', high: '0.0011', prompts: 4, verdict: 'cannot_tell' },
            latency_ms: null,
            manual: { mean: 2.5, low: 1.58, high: 3.42, prompts: 4, verdict: 'higher' },
            judge: null,
            win_rate: null,
          },
        }),
      ]),
      baseline: 'Base',
      prompts_matched: 4,
    }
    render(<ComparisonTable runIds={['r1']} charts={false} />)
    const row = screen.getByRole('row', { name: /^Better/ })
    expect(row).toHaveTextContent('95% 4.20–5.00')
    expect(row).toHaveTextContent('vs baseline +2.50')
    expect(within(row).getByText('higher')).toBeInTheDocument()
    expect(within(row).getByText('cannot tell')).toBeInTheDocument()
    expect(screen.getByRole('row', { name: /^Base/ })).toHaveTextContent('baseline')
    expect(screen.getByTestId('interval-note')).toHaveTextContent('4 matched prompts')
  })

  it('shows judge scores and the billed failures otherwise', () => {
    state.data = comparison(1, [
      candidate('A', { avg_judge_score: 4.25, judge_scored: 1, failures: 1, failures_billed: 1, attempts: 3 }),
    ])
    render(<ComparisonTable runIds={['r1']} charts={false} />)
    const row = screen.getByRole('row', { name: /^A/ })
    expect(row).toHaveTextContent('4.25')
    expect(row).toHaveTextContent('1 billed')
    expect(within(row).queryByText('withheld')).not.toBeInTheDocument()
  })
})
