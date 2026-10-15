import { render, screen, within } from '@testing-library/react'
import { describe, expect, it } from 'vitest'

import type { Attempt, CostEstimate, UsageFacts } from '../api/types'
import { TimingTable, UsageTable } from './usage'

function usage(partial: Partial<UsageFacts>): UsageFacts {
  return { api_style: 'openai_responses', cache_write_split_partial: false, input_uncached_derived: false, ...partial }
}

function valueOf(term: string): HTMLElement {
  const dt = screen.getByText(term, { selector: 'dt' })
  return dt.nextElementSibling as HTMLElement
}

describe('UsageTable', () => {
  it('says "Usage not reported." when the provider sent no usage at all', () => {
    render(<UsageTable usage={null} />)
    expect(screen.getByText('Usage not reported.')).toBeVisible()
  })

  it('shows missing details as "not reported", never as zero', () => {
    render(<UsageTable usage={usage({ input_total: 1200, output_total: 300 })} />)
    expect(valueOf('Total input')).toHaveTextContent('1,200')
    expect(valueOf('Output')).toHaveTextContent('300')
    for (const term of ['Uncached input', 'Cache read', 'Cache write', 'Reasoning (in output)']) {
      expect(valueOf(term)).toHaveTextContent('not reported')
    }
  })

  it('shows reported zeros as zeros', () => {
    render(<UsageTable usage={usage({ input_total: 10, cache_read: 0, cache_write: 0, input_uncached: 10, output_total: 5, reasoning: 0 })} />)
    expect(valueOf('Cache read')).toHaveTextContent(/^0$/)
    expect(valueOf('Reasoning (in output)')).toHaveTextContent(/^0$/)
  })

  it('marks derived uncached input and shows the Claude TTL split', () => {
    render(
      <UsageTable
        usage={usage({
          api_style: 'anthropic_messages',
          input_total: 5000,
          input_uncached: 1000,
          input_uncached_derived: true,
          cache_read: 1000,
          cache_write: 3000,
          cache_write_5m: 1000,
          cache_write_1h: 2000,
          output_total: 10,
        })}
      />,
    )
    expect(valueOf('Uncached input')).toHaveTextContent('1,000 (derived)')
    expect(valueOf('Cache write')).toHaveTextContent('3,000 (5m 1,000 / 1h 2,000)')
  })

  it('falls back to the uncached count the calculator derived', () => {
    const cost = {
      status: 'complete',
      currency: 'USD',
      lower: '0',
      total: '0',
      upper: '0',
      calculator_version: 1,
      lines: [{ category: 'input_uncached', label: 'Uncached input', tokens: 700, status: 'priced', custom_rate: false }],
    } as CostEstimate
    render(<UsageTable usage={usage({ input_total: 1000, cache_read: 300 })} cost={cost} />)
    expect(within(valueOf('Uncached input')).getByText('700')).toBeVisible()
    expect(valueOf('Uncached input')).toHaveTextContent('(derived)')
  })
})

describe('TimingTable', () => {
  it('shows latency and first-token time only for streamed responses', () => {
    const { rerender } = render(<TimingTable attempt={{ latency_ms: 1234, ttft_ms: 250, streamed: true } as Attempt} />)
    expect(valueOf('Total latency')).toHaveTextContent('1.23 s')
    expect(valueOf('First token')).toHaveTextContent('250 ms')
    rerender(<TimingTable attempt={{ latency_ms: 80, ttft_ms: null, streamed: false } as Attempt} />)
    expect(valueOf('Total latency')).toHaveTextContent('80 ms')
    expect(valueOf('First token')).toHaveTextContent('not streamed')
  })
})
