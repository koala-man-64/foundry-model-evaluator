/* Extension points filled by increment 2 (suites, judging, projections). */
import type { RunDetail } from './api/types'
import { Empty } from './components/ui'

export function JudgeSlot(_: { detail: RunDetail; promptId: string; trial: number }) {
  return null
}

export function ProjectionsSlot() {
  return <Empty>Cost projections arrive with suites and judging.</Empty>
}

export function SuitesSlot() {
  return <Empty>Saved prompt suites arrive in the next increment.</Empty>
}
