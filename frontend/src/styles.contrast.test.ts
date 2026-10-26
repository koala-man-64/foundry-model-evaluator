import css from './styles.css?raw'

/** Colour tokens of the first `:root { … }` block found after `from`. */
function tokens(from: number): Record<string, string> {
  const start = css.indexOf(':root {', from)
  const block = css.slice(start, css.indexOf('}', start))
  return Object.fromEntries([...block.matchAll(/--([\w-]+):\s*(#[0-9a-f]{6})\b/gi)].map((m) => [m[1], m[2].toLowerCase()]))
}

function luminance(hex: string): number {
  const [r, g, b] = [1, 3, 5].map((i) => {
    const c = parseInt(hex.slice(i, i + 2), 16) / 255
    return c <= 0.03928 ? c / 12.92 : ((c + 0.055) / 1.055) ** 2.4
  })
  return 0.2126 * r + 0.7152 * g + 0.0722 * b
}

function contrast(a: string, b: string): number {
  const [hi, lo] = [luminance(a), luminance(b)].sort((x, y) => y - x)
  return (hi + 0.05) / (lo + 0.05)
}

const light = tokens(0)
const dark = { ...light, ...tokens(css.indexOf('@media (prefers-color-scheme: dark)')) }

// [foreground, background, minimum]: text 4.5:1; field edges, focus rings and chart edges 3:1 (WCAG 2.2).
const PAIRS: [string, string, number][] = [
  ...['bg', 'surface', 'surface-2'].flatMap((bg): [string, string, number][] => [
    ['text', bg, 4.5],
    ['text-muted', bg, 4.5],
    ['accent', bg, 4.5],
    ['focus', bg, 3],
  ]),
  ['on-accent', 'accent', 4.5],
  ['on-accent', 'accent-hover', 4.5],
  ['control-border', 'surface', 3],
  ['control-border', 'surface-2', 3],
  ['ok', 'surface', 4.5],
  ['ok', 'surface-2', 4.5],
  ['ok', 'ok-soft', 4.5],
  ['warn', 'warn-soft', 4.5],
  ['error', 'error-soft', 4.5],
  ['custom', 'custom-soft', 4.5],
  ['accent', 'accent-soft', 4.5],
  ['text-muted', 'accent-soft', 4.5],
  ['chart-answer', 'surface', 3],
]

describe.each([
  ['light', light],
  ['dark', dark],
])('%s theme contrast', (_theme, palette) => {
  it('parses every token the pairs use', () => {
    for (const [fg, bg] of PAIRS) {
      expect(palette[fg], fg).toMatch(/^#[0-9a-f]{6}$/)
      expect(palette[bg], bg).toMatch(/^#[0-9a-f]{6}$/)
    }
  })

  it.each(PAIRS)('%s on %s is at least %s:1', (fg, bg, minimum) => {
    expect(contrast(palette[fg], palette[bg])).toBeGreaterThanOrEqual(minimum)
  })
})
