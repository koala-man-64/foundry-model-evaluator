import { describe, expect, it } from 'vitest'

import type { Attempt, RunDetail } from '../../api/types'
import { blindOrder, nextUnscoredCell, unscoredIn } from './queue'

function attempt(id: string, prompt: string, candidate: string, trial: number, status = 'succeeded'): Attempt {
  return { id, prompt_id: prompt, candidate_id: candidate, trial_index: trial, status } as Attempt
}

const attempts = [
  attempt('a11', 'p1', 'c1', 1),
  attempt('a12', 'p1', 'c2', 1),
  attempt('a21', 'p1', 'c1', 2),
  attempt('a22', 'p1', 'c2', 2, 'failed'),
  attempt('b11', 'p2', 'c1', 1),
  attempt('b12', 'p2', 'c2', 1),
  attempt('b21', 'p2', 'c1', 2),
  attempt('b22', 'p2', 'c2', 2),
]

function detail(scored: string[]): RunDetail {
  return {
    run: { trials: 2 },
    prompts: [{ id: 'p1' }, { id: 'p2' }],
    candidates: [{ id: 'c1' }, { id: 'c2' }],
    manual_scores: Object.fromEntries(scored.map((id) => [id, { attempt_id: id, score: 3, notes: '', updated_at: '' }])),
  } as unknown as RunDetail
}

const latest = new Map(attempts.map((a) => [`${a.prompt_id}|${a.candidate_id}|${a.trial_index}`, a]))

describe('scoring queue', () => {
  it('lists the succeeded answers without a manual score', () => {
    expect(unscoredIn(detail(['a11']), latest, { promptIndex: 0, trial: 1 }).map((a) => a.id)).toEqual(['a12'])
    // A failed answer cannot be scored, so it does not count as waiting.
    expect(unscoredIn(detail(['a21']), latest, { promptIndex: 0, trial: 2 })).toEqual([])
  })

  it('finds the next cell with an unscored answer, in order, wrapping around', () => {
    const done = ['a11', 'a12', 'a21', 'b11', 'b12']
    expect(nextUnscoredCell(detail(done), latest, { promptIndex: 0, trial: 1 })).toEqual({ promptIndex: 1, trial: 2 })
    expect(nextUnscoredCell(detail(['a11', 'a12', 'a21', 'b21', 'b22']), latest, { promptIndex: 1, trial: 2 })).toEqual({
      promptIndex: 1,
      trial: 1,
    })
    // Only the current cell is waiting: "next" means another cell, so there is none.
    expect(nextUnscoredCell(detail(['a11', 'a21', 'b11', 'b12', 'b21', 'b22']), latest, { promptIndex: 0, trial: 1 })).toBeNull()
    const all = ['a11', 'a12', 'a21', 'b11', 'b12', 'b21', 'b22']
    expect(nextUnscoredCell(detail(all), latest, { promptIndex: 0, trial: 1 })).toBeNull()
  })

  it('keeps a stable blind order that does not depend on the input order', () => {
    const items = [{ id: 'x1' }, { id: 'x2' }, { id: 'x3' }, { id: 'x4' }]
    const order = blindOrder(items).map((i) => i.id)
    expect(blindOrder([...items].reverse()).map((i) => i.id)).toEqual(order)
    expect([...order].sort()).toEqual(['x1', 'x2', 'x3', 'x4'])
  })
})
