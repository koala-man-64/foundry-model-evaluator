import { render, screen, within } from '@testing-library/react'
import { describe, expect, it, vi } from 'vitest'

import type { Calibration } from '../../api/types'
import { CalibrationPanel } from './CalibrationPanel'

const state: { data: Calibration | undefined } = { data: undefined }

vi.mock('../../api/hooks', () => ({
  useCalibration: () => ({ data: state.data, isLoading: false, error: null }),
}))

const base = {
  key: 'abc123def456',
  settings: 'default settings',
  prompts: 6,
  judge: 'Judge',
  rubric: 'Default v1',
  scale_min: 1,
  scale_max: 5,
  top_pick_agree: 3,
  top_pick_total: 4,
  participant_pairs_excluded: 0,
  swap_pairs: 0,
  swap_flips: 0,
  swap_consistent: 0,
}

describe('CalibrationPanel', () => {
  it('shows agreement with an interval, the bias direction and the top-pick count', () => {
    state.data = {
      run_ids: ['r'],
      min_pairs: 5,
      notes: ['Your manual scores are one person’s view, not ground truth.'],
      groups: [
        { ...base, pairs: 12, spearman: 0.71, spearman_low: 0.22, spearman_high: 0.91, within_one: 0.75, mean_bias: 0.4 },
      ],
    }
    render(<CalibrationPanel runIds={['r']} />)
    const row = screen.getByRole('row', { name: /^Judge default settings/ })
    expect(row).toHaveTextContent('0.71 (95% 0.22 to 0.91)')
    expect(row).toHaveTextContent('75%')
    expect(row).toHaveTextContent('judge higher by 0.40')
    expect(row).toHaveTextContent('3 of 4')
    expect(screen.getByText(/not ground truth/)).toBeInTheDocument()
  })

  it('says how many pairs a correlation needs instead of showing a noisy one', () => {
    state.data = {
      run_ids: ['r'],
      min_pairs: 5,
      notes: [],
      groups: [{ ...base, pairs: 3, spearman: null, spearman_low: null, spearman_high: null, within_one: 1, mean_bias: 0 }],
    }
    render(<CalibrationPanel runIds={['r']} />)
    expect(screen.getByRole('row', { name: /^Judge default settings/ })).toHaveTextContent('needs 5+ pairs')
  })

  it('explains what to do when nothing is scored by both', () => {
    state.data = { run_ids: ['r'], min_pairs: 5, notes: [], groups: [] }
    render(<CalibrationPanel runIds={['r']} />)
    expect(screen.getByText(/score some answers by hand/)).toBeInTheDocument()
  })

  it('reports order-swap flips and consistency per judge basis, and a dash without swaps', () => {
    state.data = {
      run_ids: ['r'],
      min_pairs: 5,
      notes: [],
      groups: [
        {
          ...base,
          pairs: 3,
          spearman: null,
          spearman_low: null,
          spearman_high: null,
          within_one: 1,
          mean_bias: 0,
          swap_pairs: 4,
          swap_flips: 3,
          swap_consistent: 1,
          flip_rate: 0.75,
          consistency_rate: 0.25,
        },
        {
          ...base,
          key: 'other',
          settings: 'temperature=0.5',
          pairs: 3,
          spearman: null,
          spearman_low: null,
          spearman_high: null,
          within_one: 1,
          mean_bias: 0,
        },
      ],
    }
    render(<CalibrationPanel runIds={['r']} />)
    expect(screen.getByRole('columnheader', { name: 'Order swap' })).toBeInTheDocument()
    const swapped = screen.getByRole('row', { name: /^Judge default settings/ })
    expect(swapped).toHaveTextContent('3 of 4 flipped')
    expect(swapped).toHaveTextContent('1 of 4 consistent')
    const unswapped = screen.getByRole('row', { name: /^Judge temperature=0.5/ })
    expect(within(unswapped).getAllByRole('cell').at(-1)).toHaveTextContent('—')
  })
})
