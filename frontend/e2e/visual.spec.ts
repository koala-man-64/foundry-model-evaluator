import { existsSync, mkdirSync, readdirSync, readFileSync, statSync, writeFileSync } from 'node:fs'
import { dirname, join } from 'node:path'
import { fileURLToPath } from 'node:url'

import { expect, test, type Page } from '@playwright/test'

import { AppApi, fillDemoCompare, judgeDemoRun, runDemoCompare, seedDemoProfiles, seedDemoSuite } from './helpers'

// Visual review set (not part of the regular test run). Captures the main pages in light, dark and forced colors at
// 1360px and in light at 390px, writes PNGs and page heights to FME_VISUAL_DIR (keep it outside the repository), and
// checks what a restyle must not break. Run it on its own, after `npm run build`:
//   FME_VISUAL_DIR=<folder> npx playwright test visual
// To compare page heights with an earlier capture (for example from main), also set FME_VISUAL_BASELINE=<its folder>.
const outDir = process.env.FME_VISUAL_DIR ?? ''
const baselineDir = process.env.FME_VISUAL_BASELINE ?? ''
const here = dirname(fileURLToPath(import.meta.url))

// A page may grow by at most this share of its baseline height at 1360px; anything more needs a reason in the PR.
const HEIGHT_BUDGET = 0.03

test.skip(!outDir, 'set FME_VISUAL_DIR to capture the visual review set')
test.use({ viewport: { width: 1360, height: 900 } })

type Variant = 'light' | 'dark' | 'forced' | 'narrow'

const heights: Record<string, number> = {}
const problems: string[] = []

function newestSource(dir: string): number {
  return Math.max(
    ...readdirSync(dir, { recursive: true, withFileTypes: true })
      .filter((entry) => entry.isFile())
      .map((entry) => statSync(join(entry.parentPath, entry.name)).mtimeMs),
  )
}

async function setVariant(page: Page, variant: Variant) {
  await page.setViewportSize(variant === 'narrow' ? { width: 390, height: 844 } : { width: 1360, height: 900 })
  await page.emulateMedia({ colorScheme: variant === 'dark' ? 'dark' : 'light', forcedColors: variant === 'forced' ? 'active' : 'none' })
}

/** Rendered text below 12px; SVG text is measured at its drawn size, not its user-unit size. */
async function smallText(page: Page): Promise<string[]> {
  return page.evaluate(() => {
    const found = new Set<string>()
    const walker = document.createTreeWalker(document.body, NodeFilter.SHOW_TEXT)
    for (let node = walker.nextNode(); node; node = walker.nextNode()) {
      const text = node.textContent?.trim()
      const element = node.parentElement
      if (!text || !element || element.closest('option, script, style, [hidden]')) continue
      const box = element.getBoundingClientRect()
      if (box.width <= 1 || box.height <= 1) continue // closed, collapsed or visually hidden
      let size = parseFloat(getComputedStyle(element).fontSize)
      const svg = element.closest('svg')
      const viewBox = svg?.viewBox.baseVal
      if (svg && viewBox?.width) size *= svg.getBoundingClientRect().width / viewBox.width
      if (size < 11.95) found.add(`${size.toFixed(1)}px "${text.slice(0, 40)}"`)
    }
    return [...found].slice(0, 10)
  })
}

/** Tables wider than the viewport that no ancestor lets the reader scroll. */
async function clippedTables(page: Page): Promise<string[]> {
  return page.evaluate(() =>
    Array.from(document.querySelectorAll('table'))
      .filter((table) => table.getClientRects().length > 0 && table.getBoundingClientRect().right > window.innerWidth + 1)
      .filter((table) => {
        for (let parent = table.parentElement; parent; parent = parent.parentElement) {
          if (['auto', 'scroll'].includes(getComputedStyle(parent).overflowX)) return false
        }
        return true
      })
      .map((table) => table.getAttribute('aria-label') ?? table.querySelector('th')?.textContent ?? 'table'),
  )
}

interface CaptureOptions {
  signals?: string[]
  variants?: Variant[]
  /** False for a page that belongs to none of its sub-navigation's tabs (a suite's own page). */
  ownTab?: boolean
}

async function capture(page: Page, name: string, { signals = [], variants = ['light', 'dark', 'narrow'], ownTab = true }: CaptureOptions = {}) {
  for (const variant of variants) {
    const key = `${name}--${variant}`
    await setVariant(page, variant)
    await page.waitForLoadState('networkidle')
    await page.evaluate(() => window.scrollTo(0, 0))
    heights[key] = await page.evaluate(() => document.documentElement.scrollHeight)
    await page.screenshot({ path: join(outDir, `${key}.png`), fullPage: true })

    const overflow = await page.evaluate(() => document.documentElement.scrollWidth - window.innerWidth)
    if (overflow > 0) problems.push(`${key}: the page scrolls sideways by ${overflow}px`)
    const subnavs = await page.locator('nav.subnav').evaluateAll((navs) =>
      navs.map((nav) => nav.querySelectorAll('a[aria-current="page"]').length),
    )
    if (subnavs.some((active) => active > 1 || (ownTab && active !== 1))) problems.push(`${key}: a sub-navigation has no single current tab`)
    for (const text of await smallText(page)) problems.push(`${key}: text below 12px: ${text}`)
    if (variant === 'narrow') for (const table of await clippedTables(page)) problems.push(`${key}: table clipped at 390px: ${table}`)
    for (const signal of signals) {
      if ((await page.getByText(signal, { exact: false }).filter({ visible: true }).count()) === 0) {
        problems.push(`${key}: "${signal}" is not visible`)
      }
    }
  }
  await setVariant(page, 'light')
}

test('visual review set', async ({ page, request }) => {
  test.setTimeout(420_000)
  const built = join(here, '..', 'dist', 'index.html')
  expect(existsSync(built) && statSync(built).mtimeMs > newestSource(join(here, '..', 'src')), 'dist is older than src: run npm run build').toBe(true)
  mkdirSync(outDir, { recursive: true })
  const api = new AppApi(request)

  await page.goto('/compare')
  await capture(page, 'compare-empty', { variants: ['light', 'dark'] })

  const profiles = await seedDemoProfiles(api)
  await fillDemoCompare(page)
  await capture(page, 'compare', {
    signals: ['Paid calls are made only when you press Run', 'worst-case estimated token cost'],
    variants: ['light', 'dark', 'narrow', 'forced'],
  })

  const runId = await runDemoCompare(page)
  await judgeDemoRun(api, runId, profiles.judgeId)
  await page.goto(`/runs/${runId}`)
  await expect(page.getByRole('region', { name: 'Comparison charts' }).getByTestId('cost-score-chart').locator('circle')).toHaveCount(3)
  await page.getByTestId('response-cards').getByRole('article').first().locator('summary', { hasText: 'Cost breakdown' }).click()
  await capture(page, 'run', { signals: ['Estimated token cost', 'not reported'], variants: ['light', 'dark', 'narrow', 'forced'] })
  const charts = page.getByRole('region', { name: 'Comparison charts' })
  await charts.getByRole('tab', { name: 'Token use' }).click()
  await expect(charts.getByTestId('chart-bar')).toHaveCount(3)
  await capture(page, 'run-tokens', { variants: ['light', 'dark', 'forced'] })

  await page.goto(`/scoring/${runId}`)
  await expect(page.getByTestId('evaluation').first()).toContainText('Succeeded')
  await page.getByRole('article', { name: /Score GPT-5.6 terra \(effort=high\)/ }).getByRole('button', { name: 'Score 4' }).click()
  await capture(page, 'scoring', { signals: ['A paid call.', 'Estimated token cost (judging)'], variants: ['light', 'dark', 'narrow', 'forced'] })
  await page.getByTestId('evaluation').first().getByRole('button', { name: 'Judge request' }).click()
  await capture(page, 'judge-request-dialog', { variants: ['light', 'dark'] })
  await page.getByRole('dialog').getByRole('button', { name: 'Close' }).click()
  await page.goto('/rubrics')
  await capture(page, 'rubrics', { variants: ['light', 'dark'] })

  const suiteId = await seedDemoSuite(api, profiles)
  await page.goto(`/suites/${suiteId}`)
  await expect(page.getByTestId('coverage')).toContainText('of 6 cells')
  await page.getByRole('region', { name: 'Suite runs' }).getByRole('checkbox').nth(1).check()
  await expect(page.getByRole('region', { name: 'Comparison of selected runs' })).toContainText('2 runs')
  await capture(page, 'suite', { signals: ['of 6 cells'], ownTab: false })

  await page.goto('/suites')
  await page.getByRole('button', { name: 'New suite' }).click()
  await capture(page, 'suite-editor', { variants: ['light', 'narrow'] })
  await page.goto('/history')
  await capture(page, 'history')
  await page.goto('/models/endpoints')
  await capture(page, 'endpoints')
  await page.goto('/models/profiles')
  await capture(page, 'profiles')
  await page.getByRole('button', { name: 'Add model profile' }).click()
  await capture(page, 'profile-form', { variants: ['light', 'dark'] })
  await page.goto(`/models/pricing/${profiles.terraId}`)
  await page.getByRole('button', { name: 'Match Azure prices' }).click()
  await expect(page.getByText('Match results')).toBeVisible()
  await capture(page, 'rates', { signals: ['Match results'], variants: ['light', 'dark', 'narrow', 'forced'] })
  await page.getByTestId('rate-output-0').getByRole('button', { name: 'Manual' }).click()
  await capture(page, 'rate-dialog', { variants: ['light', 'dark'] })
  await page.keyboard.press('Escape')
  await page.goto('/models/projections')
  await page.getByLabel('Requests per month').fill('250000')
  await page.getByLabel('Output length multiplier').fill('1.5')
  await expect(page.getByRole('region', { name: 'Projection' })).toBeVisible()
  await capture(page, 'projections', { signals: ['small sample'] })

  if (baselineDir) {
    const baseline = JSON.parse(readFileSync(join(baselineDir, 'heights.json'), 'utf8')) as Record<string, number>
    for (const [key, height] of Object.entries(heights)) {
      const before = baseline[key]
      if (key.endsWith('--light') && before && height > before * (1 + HEIGHT_BUDGET)) {
        problems.push(`${key}: ${before}px -> ${height}px, more than ${HEIGHT_BUDGET * 100}% taller than the baseline`)
      }
    }
  }
  writeFileSync(join(outDir, 'heights.json'), JSON.stringify(heights, null, 2))
  writeFileSync(join(outDir, 'problems.json'), JSON.stringify(problems, null, 2))
  expect(problems).toEqual([])
})
