import { describe, expect, it } from 'vitest'

import { percentToFraction } from './ProjectionsPanel'

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
