import { describe, expect, it } from 'vitest'

import { compact, decimalString, logTicks, moneyTick, median, paretoFrontier, placeLabels, publisherSlot, tokenSplit } from './chartMath'

describe('paretoFrontier', () => {
  it('keeps the points no other point beats on both cost and score', () => {
    const points = [
      { key: 'cheap-weak', x: 1, y: 2 },
      { key: 'dominated', x: 3, y: 2.5 },
      { key: 'mid', x: 2, y: 3 },
      { key: 'pricey-best', x: 10, y: 4 },
      { key: 'pricey-worse', x: 12, y: 3.5 },
    ]
    expect(paretoFrontier(points).map((p) => p.key)).toEqual(['cheap-weak', 'mid', 'pricey-best'])
  })

  it('keeps only the best score at equal cost and drops equal scores at higher cost', () => {
    const points = [
      { key: 'a', x: 1, y: 3 },
      { key: 'b', x: 1, y: 4 },
      { key: 'c', x: 2, y: 4 },
    ]
    expect(paretoFrontier(points).map((p) => p.key)).toEqual(['b'])
  })
})

describe('helpers', () => {
  it('computes medians', () => {
    expect(median([])).toBeNull()
    expect(median([3, 1, 2])).toBe(2)
    expect(median([4, 1, 2, 3])).toBe(2.5)
  })

  it('puts log ticks at 1, 2 and 5 times powers of ten', () => {
    expect(logTicks(0.0004, 0.03)).toEqual([0.0005, 0.001, 0.002, 0.005, 0.01, 0.02])
  })

  it('splits output into answer and reasoning, keeping unreported reasoning unknown', () => {
    expect(tokenSplit(100, 40)).toEqual({ answer: 60, reasoning: 40 })
    expect(tokenSplit(100, null)).toEqual({ answer: 100, reasoning: null })
    expect(tokenSplit(100, 150)).toEqual({ answer: 0, reasoning: 100 })
  })

  it('formats compact numbers and decimal strings', () => {
    expect(compact(8000)).toBe('8k')
    expect(compact(12500)).toBe('12.5k')
    expect(compact(197000)).toBe('197k')
    expect(decimalString(0.0000005)).toBe('0.0000005')
    expect(moneyTick(0.0005, 'USD')).toBe('$0.0005')
    expect(moneyTick(0.02, 'USD')).toBe('$0.02')
  })

  it('gives fixed slots to known publishers only', () => {
    expect(publisherSlot('OpenAI')).toBe(1)
    expect(publisherSlot('anthropic')).toBe(2)
    expect(publisherSlot('Google')).toBe(3)
    expect(publisherSlot('DeepSeek')).toBeNull()
  })

  it('places labels without overlaps and drops labels that fit nowhere', () => {
    const bounds = { x: 0, y: 0, width: 300, height: 100 }
    const placed = placeLabels(
      [
        { key: 'a', text: 'Alpha', x: 100, y: 50 },
        { key: 'b', text: 'Beta', x: 101, y: 51 },
      ],
      bounds,
    )
    expect(placed.map((l) => l.key)).toEqual(['a', 'b'])
    expect(new Set(placed.map((l) => `${l.anchor}|${l.lx}|${l.ly}`)).size).toBe(2)
    const cramped = placeLabels([{ key: 'c', text: 'A very long label', x: 5, y: 5 }], { x: 0, y: 0, width: 20, height: 10 })
    expect(cramped).toEqual([])
  })
})
