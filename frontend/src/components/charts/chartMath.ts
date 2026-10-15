/** Pure helpers for the comparison charts (scales, ticks, Pareto frontier, label placement). */

export interface Point {
  key: string
  x: number
  y: number
}

/** Points no other point beats on both axes (lower x and higher y), ordered by x. */
export function paretoFrontier<T extends Point>(points: T[]): T[] {
  const sorted = [...points].sort((a, b) => a.x - b.x || b.y - a.y)
  const frontier: T[] = []
  let best = -Infinity
  for (const point of sorted) {
    if (point.y > best) {
      frontier.push(point)
      best = point.y
    }
  }
  return frontier
}

export function median(values: number[]): number | null {
  if (values.length === 0) return null
  const sorted = [...values].sort((a, b) => a - b)
  const middle = Math.floor(sorted.length / 2)
  return sorted.length % 2 ? sorted[middle] : (sorted[middle - 1] + sorted[middle]) / 2
}

/** "Nice" linear ticks covering [min, max]. */
export function linearTicks(min: number, max: number, count = 5): number[] {
  if (!Number.isFinite(min) || !Number.isFinite(max)) return []
  if (max <= min) return [min]
  const raw = (max - min) / count
  const magnitude = 10 ** Math.floor(Math.log10(raw))
  const step = [1, 2, 2.5, 5, 10].map((m) => m * magnitude).find((s) => s >= raw) ?? raw
  const ticks: number[] = []
  for (let value = Math.ceil(min / step) * step; value <= max + step / 1e6; value += step) {
    ticks.push(Number(value.toPrecision(12)))
  }
  return ticks
}

/** Domain [min, max] extended to whole ticks. */
export function niceDomain(min: number, max: number, count = 5): [number, number] {
  if (max <= min) {
    const pad = Math.abs(min) * 0.1 || 1
    return [min - pad, max + pad]
  }
  const raw = (max - min) / count
  const magnitude = 10 ** Math.floor(Math.log10(raw))
  const step = [1, 2, 2.5, 5, 10].map((m) => m * magnitude).find((s) => s >= raw) ?? raw
  return [Math.floor(min / step) * step, Math.ceil(max / step) * step]
}

/** Log-scale ticks at 1, 2 and 5 times powers of ten inside [min, max] (min > 0). */
export function logTicks(min: number, max: number): number[] {
  const ticks: number[] = []
  for (let power = Math.floor(Math.log10(min)); power <= Math.ceil(Math.log10(max)); power++) {
    for (const m of [1, 2, 5]) {
      const value = Number((m * 10 ** power).toPrecision(12))
      if (value >= min && value <= max) ticks.push(value)
    }
  }
  return ticks
}

export function scaleLinear(domain: [number, number], range: [number, number]) {
  const [d0, d1] = domain
  const [r0, r1] = range
  return (value: number) => (d1 === d0 ? (r0 + r1) / 2 : r0 + ((value - d0) / (d1 - d0)) * (r1 - r0))
}

export function scaleLog(domain: [number, number], range: [number, number]) {
  const linear = scaleLinear([Math.log10(domain[0]), Math.log10(domain[1])], range)
  return (value: number) => linear(Math.log10(value))
}

/** Answer and reasoning parts of the output; reasoning is null when the provider did not report it. */
export function tokenSplit(output: number, reasoning: number | null | undefined): { answer: number; reasoning: number | null } {
  if (reasoning === null || reasoning === undefined) return { answer: output, reasoning: null }
  const part = Math.min(Math.max(reasoning, 0), output)
  return { answer: output - part, reasoning: part }
}

export function compact(value: number): string {
  if (Math.abs(value) >= 1_000_000) return `${trim(value / 1_000_000)}M`
  if (Math.abs(value) >= 1000) return `${trim(value / 1000)}k`
  return trim(value)
}

function trim(value: number): string {
  return value >= 100 || Number.isInteger(value) ? Math.round(value).toString() : value.toFixed(1).replace(/\.0$/, '')
}

export function truncate(text: string, length: number): string {
  return text.length > length ? `${text.slice(0, length - 1)}…` : text
}

export interface Box {
  x: number
  y: number
  width: number
  height: number
}

export interface LabelRequest {
  key: string
  text: string
  x: number
  y: number
}

export interface PlacedLabel extends LabelRequest {
  anchor: 'start' | 'end' | 'middle'
  lx: number
  ly: number
}

const CHAR_WIDTH = 6.2
const LINE_HEIGHT = 12

function overlaps(a: Box, b: Box): boolean {
  return a.x < b.x + b.width && b.x < a.x + a.width && a.y < b.y + b.height && b.y < a.y + a.height
}

/**
 * Places each label next to its point (right, left, above, below), skipping positions that collide with
 * labels already placed or leave the plot. Labels that fit nowhere are dropped (the tooltip and table
 * still name every point).
 */
export function placeLabels(requests: LabelRequest[], bounds: Box, offset = 8): PlacedLabel[] {
  const placed: PlacedLabel[] = []
  const boxes: Box[] = []
  for (const request of requests) {
    const width = request.text.length * CHAR_WIDTH
    const candidates: { anchor: PlacedLabel['anchor']; lx: number; ly: number; box: Box }[] = [
      { anchor: 'start', lx: request.x + offset, ly: request.y + 4, box: { x: request.x + offset, y: request.y - 8, width, height: LINE_HEIGHT } },
      { anchor: 'end', lx: request.x - offset, ly: request.y + 4, box: { x: request.x - offset - width, y: request.y - 8, width, height: LINE_HEIGHT } },
      { anchor: 'middle', lx: request.x, ly: request.y - offset - 2, box: { x: request.x - width / 2, y: request.y - offset - 12, width, height: LINE_HEIGHT } },
      { anchor: 'middle', lx: request.x, ly: request.y + offset + 10, box: { x: request.x - width / 2, y: request.y + offset, width, height: LINE_HEIGHT } },
    ]
    const choice = candidates.find(
      (c) =>
        c.box.x >= bounds.x &&
        c.box.x + c.box.width <= bounds.x + bounds.width &&
        c.box.y >= bounds.y &&
        c.box.y + c.box.height <= bounds.y + bounds.height &&
        !boxes.some((b) => overlaps(b, c.box)),
    )
    if (choice) {
      placed.push({ ...request, anchor: choice.anchor, lx: choice.lx, ly: choice.ly })
      boxes.push(choice.box)
    }
  }
  return placed
}

/** Fixed colour slots for well-known publishers; every other publisher is "Other" (neutral). */
export const PUBLISHER_SLOTS: Record<string, number> = { openai: 1, anthropic: 2, google: 3 }

export function publisherSlot(publisher: string): number | null {
  return PUBLISHER_SLOTS[publisher.trim().toLowerCase()] ?? null
}

/** A plain decimal string (no exponent) for a positive tick value, for the money formatter. */
export function decimalString(value: number): string {
  return value.toFixed(12).replace(/0+$/, '').replace(/\.$/, '')
}

/** A short money label for an axis tick, e.g. "$0.0005" or "$0.02". */
export function moneyTick(value: number, currency: string): string {
  return new Intl.NumberFormat('en-US', { style: 'currency', currency, maximumSignificantDigits: 2 }).format(value)
}
