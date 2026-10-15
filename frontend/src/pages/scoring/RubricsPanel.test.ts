import { describe, expect, it } from 'vitest'

import { rubricProblems } from './RubricsPanel'

const criterion = (key: string, weight = '1', name = 'Name') => ({ key, name, description: '', weight })

describe('rubricProblems', () => {
  it('accepts a valid rubric', () => {
    expect(rubricProblems(1, 5, [criterion('correctness'), criterion('clarity', '2')])).toEqual([])
  })

  it('mirrors the server rules', () => {
    expect(rubricProblems(5, 5, [criterion('a')])).toContain('The scale minimum must be lower than the maximum.')
    expect(rubricProblems(1.5, 5, [criterion('a')])).toContain('The score scale uses whole numbers.')
    expect(rubricProblems(1, 5, [])).toContain('Add at least one criterion.')
    expect(rubricProblems(1, 5, [criterion('a'), criterion('a')])).toContain('Criterion 2: the key "a" is used twice.')
    expect(rubricProblems(1, 5, [criterion('Bad Key')])[0]).toMatch(/lowercase letters/)
    expect(rubricProblems(1, 5, [criterion('a', '0')])).toContain('Criterion 1: the weight must be greater than zero.')
    expect(rubricProblems(1, 5, [criterion('a', '1', ' ')])).toContain('Criterion 1: give it a name.')
  })
})
