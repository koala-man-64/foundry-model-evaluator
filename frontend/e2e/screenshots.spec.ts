import { mkdirSync } from 'node:fs'
import { dirname, join } from 'node:path'
import { fileURLToPath } from 'node:url'

import { expect, test, type Page } from '@playwright/test'

import { AppApi, fillDemoCompare, judgeDemoRun, runDemoCompare, seedDemoProfiles, seedDemoSuite } from './helpers'

// Regenerates docs/screenshots (not part of the regular test run): FME_SCREENSHOTS=1 npx playwright test screenshots
const here = dirname(fileURLToPath(import.meta.url))
const outDir = join(here, '..', '..', 'docs', 'screenshots')

test.skip(!process.env.FME_SCREENSHOTS, 'set FME_SCREENSHOTS=1 to regenerate the documentation screenshots')
test.use({ viewport: { width: 1360, height: 900 } })

async function shot(page: Page, name: string) {
  await page.waitForLoadState('networkidle')
  await page.evaluate(() => window.scrollTo(0, 0)) // keep the sticky header at the top
  await page.screenshot({ path: join(outDir, `${name}.png`), fullPage: true })
}

test('documentation screenshots', async ({ page, request }) => {
  test.setTimeout(240_000)
  mkdirSync(outDir, { recursive: true })
  const api = new AppApi(request)
  const profiles = await seedDemoProfiles(api)

  // Compare page with three candidates.
  await fillDemoCompare(page)
  await shot(page, 'compare')

  const runId = await runDemoCompare(page)
  await page.getByTestId('response-cards').getByRole('article').first().locator('summary', { hasText: 'Cost breakdown' }).click()
  await shot(page, 'run')

  // Judge the run.
  await judgeDemoRun(api, runId, profiles.judgeId)
  await page.goto(`/runs/${runId}`)
  const charts = page.getByRole('region', { name: 'Comparison charts' })
  await expect(charts.getByTestId('cost-score-chart').locator('circle')).toHaveCount(3)
  await charts.screenshot({ path: join(outDir, 'chart-quality.png') })
  await charts.getByRole('tab', { name: 'Token use' }).click()
  await expect(charts.getByTestId('chart-bar')).toHaveCount(3)
  await charts.screenshot({ path: join(outDir, 'chart-tokens.png') })
  if (process.env.FME_DARK_CHECK) {
    // Not documentation: a dark-mode render to check contrast and labels.
    await page.emulateMedia({ colorScheme: 'dark' })
    await charts.screenshot({ path: join(here, '..', 'test-results', 'chart-tokens-dark.png') })
    await charts.getByRole('tab', { name: 'Quality vs cost' }).click()
    await charts.screenshot({ path: join(here, '..', 'test-results', 'chart-quality-dark.png') })
    await page.emulateMedia({ colorScheme: 'light' })
  }
  await page.goto(`/scoring/${runId}`)
  await expect(page.getByTestId('evaluation').first()).toContainText('Succeeded')
  await page.getByRole('article', { name: /Score GPT-5.6 terra \(effort=high\)/ }).getByRole('button', { name: 'Score 4' }).click()
  await expect(page.getByRole('region', { name: 'Judge' }).getByText('The judge also took part')).toHaveCount(0)
  await shot(page, 'scoring')

  // A suite with two runs and its comparison.
  const suiteId = await seedDemoSuite(api, profiles)
  await page.goto(`/suites/${suiteId}`)
  await expect(page.getByTestId('coverage')).toContainText('of 6 cells') // the latest run is selected first
  await page.getByRole('region', { name: 'Suite runs' }).getByRole('checkbox').nth(1).check()
  await expect(page.getByRole('region', { name: 'Comparison of selected runs' })).toContainText('2 runs')
  await shot(page, 'suite')

  // Rates and projections.
  await page.goto(`/models/pricing/${profiles.terraId}`)
  await page.getByRole('button', { name: 'Match Azure prices' }).click()
  await expect(page.getByText('Match results')).toBeVisible()
  await shot(page, 'rates')
  await page.goto('/models/projections')
  await page.getByLabel('Requests per month').fill('250000')
  await page.getByLabel('Output length multiplier').fill('1.5')
  await expect(page.getByRole('region', { name: 'Projection' })).toBeVisible()
  await shot(page, 'projections')
})
