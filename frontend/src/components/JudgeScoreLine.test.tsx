import { render, screen } from '@testing-library/react'
import { describe, expect, it } from 'vitest'

import type { JudgeAggregate, JudgeScore } from '../api/types'
import { JudgeScoreLine } from './ResponseCard'

const latest: JudgeScore = {
  evaluation_id: 'ev-2',
  label: 'A',
  weighted_overall: 4.25,
  judge_overall: 4.25,
  preferred: true,
  tie: false,
  judge_is_participant: false,
}

function aggregate(partial: Partial<JudgeAggregate>): JudgeAggregate {
  return {
    mean_weighted_overall: 4.25,
    n_evaluations: 1,
    spread: null,
    win_share: 1,
    evaluation_ids: ['ev-2'],
    left_out: 0,
    ...partial,
  }
}

describe('JudgeScoreLine', () => {
  it('adds what comparisons use when an answer was judged more than once', () => {
    render(
      <JudgeScoreLine
        score={latest}
        aggregate={aggregate({ mean_weighted_overall: 4.125, n_evaluations: 2, spread: 0.25, evaluation_ids: ['ev-1', 'ev-2'] })}
      />,
    )
    expect(screen.getByTestId('judge-score')).toHaveTextContent('4.25(latest evaluation')
    expect(screen.getByTestId('judge-aggregate')).toHaveTextContent(
      'Comparisons use 4.13: the mean of 2 evaluations on one judge, rubric version and settings; range 0.25.',
    )
  })

  it('names evaluations left out for another basis, even when the latest one is not used', () => {
    render(<JudgeScoreLine score={latest} aggregate={aggregate({ mean_weighted_overall: 3.5, evaluation_ids: ['ev-1'], left_out: 1 })} />)
    expect(screen.getByTestId('judge-aggregate')).toHaveTextContent('Comparisons use 3.50: the mean of 1 evaluation')
    expect(screen.getByTestId('judge-aggregate')).toHaveTextContent('1 on another basis left out')
  })

  it('says nothing more when the latest evaluation is all there is', () => {
    render(<JudgeScoreLine score={latest} aggregate={aggregate({})} />)
    expect(screen.queryByTestId('judge-aggregate')).not.toBeInTheDocument()
  })
})
