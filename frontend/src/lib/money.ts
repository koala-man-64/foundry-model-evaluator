/**
 * Decimal-string formatting for money. Amounts arrive from the API as exact decimal strings and
 * are rounded here for display only, using integer (BigInt) arithmetic — never binary floats.
 */

interface ParsedDecimal {
  negative: boolean
  digits: bigint // value * 10^scale
  scale: number
}

export function parseDecimal(value: string): ParsedDecimal | null {
  const match = /^\s*(-)?(\d*)(?:\.(\d*))?\s*$/.exec(value)
  if (!match || (match[2] === '' && (match[3] ?? '') === '')) return null
  const intPart = match[2] || '0'
  const frac = match[3] ?? ''
  return { negative: match[1] === '-', digits: BigInt(intPart + frac), scale: frac.length }
}

/** Round half up (away from zero) to `places` decimals and return a plain decimal string. */
export function roundDecimal(value: string, places: number): string {
  const parsed = parseDecimal(value)
  if (!parsed) return value
  let { digits } = parsed
  const { scale, negative } = parsed
  if (scale > places) {
    const factor = 10n ** BigInt(scale - places)
    const quotient = digits / factor
    const remainder = digits % factor
    digits = remainder * 2n >= factor ? quotient + 1n : quotient
  } else {
    digits = digits * 10n ** BigInt(places - scale)
  }
  let text = digits.toString().padStart(places + 1, '0')
  if (places > 0) text = `${text.slice(0, -places)}.${text.slice(-places)}`
  const isZero = digits === 0n
  return negative && !isZero ? `-${text}` : text
}

function groupThousands(integer: string): string {
  return integer.replace(/\B(?=(\d{3})+(?!\d))/g, ',')
}

/** Decimal places that keep small per-request amounts readable. */
export function displayPlaces(value: string): number {
  const parsed = parseDecimal(value)
  if (!parsed || parsed.digits === 0n) return 2
  const absolute = parsed.digits
  const one = 10n ** BigInt(parsed.scale)
  if (absolute >= one) return 2 // >= 1
  if (absolute * 100n >= one) return 4 // >= 0.01
  return 6
}

export function formatMoney(value: string | null | undefined, currency = 'USD', places?: number): string {
  if (value === null || value === undefined || value === '') return '—'
  const rounded = roundDecimal(value, places ?? displayPlaces(value))
  const negative = rounded.startsWith('-')
  const [integer, fraction] = (negative ? rounded.slice(1) : rounded).split('.')
  const symbol = currency === 'USD' ? '$' : `${currency} `
  return `${negative ? '-' : ''}${symbol}${groupThousands(integer)}${fraction !== undefined ? `.${fraction}` : ''}`
}

export function isZeroDecimal(value: string | null | undefined): boolean {
  if (!value) return false
  const parsed = parseDecimal(value)
  return parsed !== null && parsed.digits === 0n
}
