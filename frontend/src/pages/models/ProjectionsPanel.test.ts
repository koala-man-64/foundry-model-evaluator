import { describe, expect, it } from 'vitest'

import { percentToFraction, validRatio } from './ProjectionsPanel'

describe('percentToFraction', () => {
  it('converts percentages exactly with decimal strings', () => {
    expect(percentToFraction('30')).toBe('0.30')
    expect(percentToFraction('12.5')).toBe('0.125')
    expect(percentToFraction('0')).toBe('0.00')
    expect(percentToFraction('100')).toBe('1.00')
    expect(percentToFraction('0.1')).toBe('0.001')
  })

  it('rejects negative or invalid input', () => {
    expect(percentToFraction('-5')).toBeNull()
    expect(percentToFraction('abc')).toBeNull()
    expect(percentToFraction('')).toBeNull()
  })
})

describe('validRatio', () => {
  it('accepts a batch price ratio greater than 0 and at most 1', () => {
    for (const text of ['0.5', '1', '1.000', '0.0001', ' .25 ']) expect(validRatio(text)).toBe(true)
  })

  it('rejects zero, negatives, ratios above 1 and invalid input', () => {
    for (const text of ['0', '0.000', '-0.5', '1.0001', '2', 'half', '']) expect(validRatio(text)).toBe(false)
  })
})
