import { render, screen } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { describe, expect, it } from 'vitest'

import type { CostEstimate, CostTotal } from '../api/types'
import { CostDetails, CostValue, TotalBox } from './cost'

function estimate(partial: Partial<CostEstimate>): CostEstimate {
  return { status: 'complete', currency: 'USD', lower: '0', total: null, upper: null, calculator_version: 1, ...partial }
}

function total(partial: Partial<CostTotal>): CostTotal {
  return { status: 'complete', currency: 'USD', lower: '0', total: null, upper: null, counted: 1, incomplete: 0, pending: 0, other_currency: 0, ...partial }
}

describe('CostValue', () => {
  it('shows a complete estimate as an exact amount', () => {
    render(<CostValue cost={estimate({ total: '0.0012345', lower: '0.0012345', upper: '0.0012345' })} />)
    expect(screen.getByText('$0.001235')).toBeVisible()
    expect(screen.queryByText('incomplete')).toBeNull()
  })

  it('shows an incomplete estimate as a lower bound, never as a plain total', () => {
    render(<CostValue cost={estimate({ status: 'incomplete', lower: '0.01', upper: '0.02', reasons: ['cache writes not reported'] })} />)
    const value = screen.getByText(/≥ \$0\.0100/)
    expect(value).toHaveTextContent('≥ $0.0100 (≤ $0.0200) incomplete')
    expect(value).toHaveAttribute('title', 'cache writes not reported')
  })

  it('keeps unknown usage incomplete even when the known part is zero', () => {
    render(<CostValue cost={estimate({ status: 'incomplete', lower: '0' })} />)
    expect(screen.getByText(/≥ \$0\.00/)).toHaveTextContent('≥ $0.00 incomplete')
  })

  it('shows $0.00 only for requests that were never sent', () => {
    render(<CostValue cost={estimate({ status: 'not_sent', lower: '0', total: '0' })} />)
    expect(screen.getByText(/\$0\.00/)).toHaveTextContent('$0.00 (not sent)')
  })

  it('shows "pending" before an estimate exists', () => {
    render(<CostValue cost={null} />)
    expect(screen.getByText('pending')).toBeVisible()
  })
})

describe('CostDetails', () => {
  it('lists every category with its tokens, rate, source and reasons', async () => {
    render(
      <CostDetails
        cost={estimate({
          status: 'incomplete',
          lower: '0.003',
          reasons: ['Cache write tokens were not reported.'],
          assumptions: ['Cache writes use the requested 5m TTL (inferred).'],
          lines: [
            { category: 'input_uncached', label: 'Uncached input', tokens: 1000, status: 'priced', rate_per_million: '2.5', cost: '0.0025', source: 'azure_auto', custom_rate: false },
            { category: 'cache_read', label: 'Cache read', tokens: 200, status: 'folded', rate_per_million: '2.5', cost: '0.0005', custom_rate: false },
            { category: 'cache_write', label: 'Cache write', tokens: null, status: 'unknown_rate', custom_rate: false },
            { category: 'output', label: 'Output', tokens: 100, status: 'priced', rate_per_million: '20', cost: '0.002', source: 'custom_override', custom_rate: true },
          ],
        })}
      />,
    )
    await userEvent.click(screen.getByText(/Cost breakdown/))
    expect(screen.getByText('Cost breakdown · includes custom rates')).toBeVisible()
    expect(screen.getByText('not reported')).toBeVisible()
    expect(screen.getByText('billed as uncached input')).toBeVisible()
    expect(screen.getByText('no rate')).toBeVisible()
    expect(screen.getByText('Custom rate')).toBeVisible()
    expect(screen.getByText('Azure (auto)')).toBeVisible()
    expect(screen.getByText('Why the estimate is incomplete')).toBeVisible()
    expect(screen.getByText('Cache write tokens were not reported.')).toBeVisible()
    expect(screen.getByText(/Cache writes use the requested 5m TTL/)).toBeVisible()
  })

  it('renders nothing for requests that were never sent', () => {
    const { container } = render(<CostDetails cost={estimate({ status: 'not_sent' })} />)
    expect(container).toBeEmptyDOMElement()
  })
})

describe('TotalBox', () => {
  it('labels every total as an estimated token cost', () => {
    render(
      <>
        <TotalBox title="Generation" total={total({ total: '1.5', lower: '1.5', upper: '1.5' })} />
        <TotalBox title="Judging" total={total({ status: 'empty', counted: 0 })} />
        <TotalBox title="Combined" total={total({ status: 'incomplete', lower: '1.5', upper: '2', counted: 3, incomplete: 1 })} />
      </>,
    )
    expect(screen.getAllByText(/Estimated token cost/)).toHaveLength(3)
    expect(screen.getByText('$1.50')).toBeVisible()
    expect(screen.getByText('$0.00')).toBeVisible()
    expect(screen.getByText('Estimated token cost · nothing yet')).toBeVisible()
    expect(screen.getByText('≥ $1.50')).toBeVisible()
    expect(screen.getByText('Estimated token cost · 1 of 3 incomplete · ≤ $2.00')).toBeVisible()
  })

  it('says when amounts in another currency are left out', () => {
    render(<TotalBox title="Judging" total={total({ status: 'incomplete', lower: '0.1', counted: 1, other_currency: 2 })} />)
    expect(screen.getByText('≥ $0.1000')).toBeVisible()
    expect(screen.getByText('Estimated token cost · 2 in another currency not included')).toBeVisible()
  })

  it('shows pending calls as a lower bound', () => {
    render(<TotalBox title="Generation" total={total({ status: 'pending', lower: '0.2', pending: 2 })} />)
    expect(screen.getByText('≥ $0.2000')).toBeVisible()
    expect(screen.getByText('Estimated token cost · 2 pending')).toBeVisible()
  })
})
