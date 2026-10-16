import { render, screen, within } from '@testing-library/react'
import { describe, expect, it } from 'vitest'

import type { CandidateProjection, ProjectionResult } from '../../api/types'
import { CostSpreadTable } from './CostSpreadTable'

function candidate(label: string, partial: Partial<CandidateProjection> = {}): CandidateProjection {
  return {
    candidate_key: label,
    label,
    profile_name: label,
    attempts_used: 12,
    attempts_excluded: 3,
    exclusion_reasons: ['3 excluded: did not succeed'],
    cost_per_request: '0.0021',
    cost_per_1k_requests: '2.1',
    cost_per_month: '21',
    cost_spread: {
      p50: '0.0018',
      p90: '0.0042',
      p95: '0.0051',
      max: '0.0093',
      month_if_every_request_p50: '18',
      month_if_every_request_p95: '51',
      basis: 'nearest-rank over 12 requests with complete estimates (3 excluded)',
      sample_warning: 'Only 12 requests priced: percentiles from fewer than 30 are rough.',
    },
    status: 'complete',
    assumptions: [],
    warnings: [],
    custom_rates: false,
    cache_read_applicable: true,
    cache_write_applicable: false,
    ...partial,
  }
}

function result(candidates: CandidateProjection[]): ProjectionResult {
  return {
    currency: 'USD',
    price_basis: 'original',
    monthly_requests: 10000,
    what_if: { input_multiplier: '1', output_multiplier: '1', cache_write_ttl: '5m' },
    assumptions: [],
    candidates,
    notes: [],
  }
}

describe('CostSpreadTable', () => {
  it('shows the percentiles and labels the monthly figures as if every request cost them', () => {
    render(<CostSpreadTable result={result([candidate('Model A')])} />)
    expect(screen.getByRole('columnheader', { name: 'Per month if every request cost the p50' })).toBeInTheDocument()
    expect(screen.getByRole('columnheader', { name: 'Per month if every request cost the p95' })).toBeInTheDocument()
    expect(screen.getByText(/10,000 requests would cost if every request cost the p50 or the p95; they are not a forecast range/)).toBeInTheDocument()
    const cells = within(screen.getByTestId('projection-spread-row')).getAllByRole('cell')
    expect(cells.map((cell) => cell.textContent)).toEqual([
      'Model A',
      '$0.001800',
      '$0.004200',
      '$0.005100',
      '$0.009300',
      '$18.00',
      '$51.00',
      'nearest-rank over 12 requests with complete estimates (3 excluded)small sample Only 12 requests priced: percentiles from fewer than 30 are rough.',
    ])
  })

  it('shows an unknown spread as a dash, never as zero', () => {
    const unavailable = candidate('Model B', {
      status: 'unavailable',
      attempts_used: 0,
      cost_per_request: null,
      cost_per_1k_requests: null,
      cost_per_month: null,
      cost_spread: null,
    })
    render(<CostSpreadTable result={result([unavailable])} />)
    const cells = within(screen.getByTestId('projection-spread-row')).getAllByRole('cell')
    expect(cells.slice(1, 7).map((cell) => cell.textContent)).toEqual(['—', '—', '—', '—', '—', '—'])
    expect(cells[7]).toHaveTextContent('no request priced completely')
  })

  it('has no sample warning from 30 priced requests', () => {
    const spread = { ...candidate('Model C').cost_spread!, sample_warning: null }
    render(<CostSpreadTable result={result([candidate('Model C', { cost_spread: spread })])} />)
    expect(screen.queryByText('small sample')).not.toBeInTheDocument()
  })
})
