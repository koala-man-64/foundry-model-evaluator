import type { Attempt, RunDetail } from '../../api/types'

export interface CellRef {
  promptIndex: number
  trial: number
}

/** The succeeded answers of a cell that have no manual score yet. */
export function unscoredIn(detail: RunDetail, latest: Map<string, Attempt>, cell: CellRef): Attempt[] {
  const prompt = detail.prompts[cell.promptIndex]
  if (!prompt) return []
  return detail.candidates
    .map((c) => latest.get(`${prompt.id}|${c.id}|${cell.trial}`))
    .filter((a): a is Attempt => !!a && a.status === 'succeeded' && !detail.manual_scores[a.id])
}

/** The next cell (prompt × trial, in order, wrapping around) with an unscored answer, or null if none. */
export function nextUnscoredCell(detail: RunDetail, latest: Map<string, Attempt>, from: CellRef): CellRef | null {
  const trials = Math.max(1, detail.run.trials)
  const total = detail.prompts.length * trials
  const start = from.promptIndex * trials + (from.trial - 1)
  for (let step = 1; step <= total; step++) {
    const index = (start + step) % total
    const cell = { promptIndex: Math.floor(index / trials), trial: (index % trials) + 1 }
    if (unscoredIn(detail, latest, cell).length > 0) return cell
  }
  return null
}

function hash(text: string): number {
  let h = 2166136261
  for (let i = 0; i < text.length; i++) {
    h ^= text.charCodeAt(i)
    h = Math.imul(h, 16777619)
  }
  return h >>> 0
}

/** A stable order for blind review that does not follow the candidate order, so position gives nothing away. */
export function blindOrder<T extends { id: string }>(items: T[]): T[] {
  return [...items].sort((a, b) => hash(a.id) - hash(b.id) || a.id.localeCompare(b.id))
}

export const BLIND_LABELS = 'ABCDEFGHIJKLMNOP'
