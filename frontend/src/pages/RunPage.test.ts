import { describe, expect, it } from 'vitest'

import type { Attempt } from '../api/types'
import { latestAttempts } from './RunPage'

function attempt(id: string, candidate: string, created: string, trial = 1): Attempt {
  return { id, prompt_id: 'p1', candidate_id: candidate, trial_index: trial, created_at: created } as Attempt
}

describe('latestAttempts', () => {
  it('shows the newest attempt per prompt, candidate and trial (explicit retries replace the view, not the record)', () => {
    const latest = latestAttempts([
      attempt('a1', 'c1', '2026-10-01T10:00:00Z'),
      attempt('a2', 'c1', '2026-10-01T10:05:00Z'),
      attempt('b1', 'c2', '2026-10-01T10:00:00Z'),
      attempt('b2', 'c2', '2026-10-01T10:00:00Z', 2),
    ])
    expect(latest.get('p1|c1|1')?.id).toBe('a2')
    expect(latest.get('p1|c2|1')?.id).toBe('b1')
    expect(latest.get('p1|c2|2')?.id).toBe('b2')
    expect(latest.size).toBe(3)
  })
})
