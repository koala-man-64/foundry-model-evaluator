import { describe, expect, it } from 'vitest'

import { displayPlaces, formatMoney, isZeroDecimal, parseDecimal, roundDecimal } from './money'

describe('parseDecimal', () => {
  it('parses plain decimal strings exactly', () => {
    expect(parseDecimal('2.50')).toEqual({ negative: false, digits: 250n, scale: 2 })
    expect(parseDecimal(' -0.125 ')).toEqual({ negative: true, digits: 125n, scale: 3 })
    expect(parseDecimal('.5')).toEqual({ negative: false, digits: 5n, scale: 1 })
    expect(parseDecimal('7')).toEqual({ negative: false, digits: 7n, scale: 0 })
  })

  it('rejects anything that is not a plain decimal', () => {
    for (const value of ['', '.', 'abc', '1e5', '1,000', '--1', '0x10', 'NaN']) expect(parseDecimal(value)).toBeNull()
  })
})

describe('roundDecimal', () => {
  it('rounds half up, away from zero, without binary floats', () => {
    expect(roundDecimal('0.125', 2)).toBe('0.13')
    expect(roundDecimal('-0.125', 2)).toBe('-0.13')
    // 0.285 is 0.28499999… as a binary float; decimal rounding must give 0.29.
    expect(roundDecimal('0.285', 2)).toBe('0.29')
    expect(roundDecimal('1.5', 0)).toBe('2')
    expect(roundDecimal('0.0049', 2)).toBe('0.00')
  })

  it('pads and never produces negative zero', () => {
    expect(roundDecimal('3', 2)).toBe('3.00')
    expect(roundDecimal('-0.004', 2)).toBe('0.00')
  })

  it('keeps precision for very small and very large amounts', () => {
    expect(roundDecimal('0.0000004999', 6)).toBe('0.000000')
    expect(roundDecimal('0.0000005', 6)).toBe('0.000001')
    expect(roundDecimal('123456789012345678.995', 2)).toBe('123456789012345679.00')
  })
})

describe('formatMoney', () => {
  it('chooses readable precision for small amounts', () => {
    expect(displayPlaces('12.3')).toBe(2)
    expect(displayPlaces('0.05')).toBe(4)
    expect(displayPlaces('0.000123')).toBe(6)
    expect(displayPlaces('0')).toBe(2)
    expect(formatMoney('0.0012345')).toBe('$0.001235')
    expect(formatMoney('0.05')).toBe('$0.0500')
  })

  it('groups thousands and labels non-USD currencies', () => {
    expect(formatMoney('1234567.891')).toBe('$1,234,567.89')
    expect(formatMoney('-1234.5')).toBe('-$1,234.50')
    expect(formatMoney('1', 'EUR')).toBe('EUR 1.00')
    expect(formatMoney('2.5', 'USD', 4)).toBe('$2.5000')
  })

  it('shows a dash for missing amounts instead of zero', () => {
    expect(formatMoney(null)).toBe('—')
    expect(formatMoney(undefined)).toBe('—')
    expect(formatMoney('')).toBe('—')
  })
})

describe('isZeroDecimal', () => {
  it('detects exact zero only', () => {
    expect(isZeroDecimal('0.000')).toBe(true)
    expect(isZeroDecimal('0.0001')).toBe(false)
    expect(isZeroDecimal(null)).toBe(false)
    expect(isZeroDecimal('not a number')).toBe(false)
  })
})
