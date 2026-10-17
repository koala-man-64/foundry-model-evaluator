import { render, screen } from '@testing-library/react'
import { describe, expect, it } from 'vitest'

import type { CheckOutcome } from '../api/types'
import { CheckResultsLine } from './ResponseCard'

describe('CheckResultsLine', () => {
  it('tells "not checked yet" apart from "could not check"', () => {
    const pending: CheckOutcome = {
      passed: null,
      results: [
        { kind: 'contains', passed: true, detail: 'answer contains "x"', not_checked: false },
        { kind: 'regex', passed: null, detail: 'not checked yet: reload to continue', not_checked: true },
      ],
    }
    const { unmount } = render(<CheckResultsLine outcome={pending} />)
    expect(screen.getByText('not checked yet')).toBeInTheDocument()
    expect(screen.getByText('reload to continue')).toBeInTheDocument()
    unmount()

    const unknown: CheckOutcome = {
      passed: null,
      results: [{ kind: 'regex', passed: null, detail: 'could not check: took too long', not_checked: false }],
    }
    render(<CheckResultsLine outcome={unknown} />)
    expect(screen.getByText('could not check')).toBeInTheDocument()
    expect(screen.getByText('0 of 1')).toBeInTheDocument()
  })
})
