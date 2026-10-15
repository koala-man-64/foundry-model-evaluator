import { mkdirSync } from 'node:fs'
import { dirname, join } from 'node:path'
import { fileURLToPath } from 'node:url'

import { expect, test, type Page } from '@playwright/test'

import { AppApi, candidateCard, chooseProfile } from './helpers'

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
  const endpointId = await api.endpoint('Contoso Foundry (East US 2)')
  const terraId = await api.profile({
    endpointId,
    name: 'GPT-5.6 terra',
    apiStyle: 'openai_responses',
    preset: 'openai-reasoning-cache-write',
    deployment: 'gpt-5.6-terra',
    meterTerms: '5.6 terra',
    longContextThreshold: 272000,
    extra: { publisher: 'OpenAI', model_name: 'gpt-5.6-terra', context_window: 400000 },
  })
  const claudeId = await api.profile({
    endpointId,
    name: 'Claude Sonnet',
    apiStyle: 'anthropic_messages',
    preset: 'claude-adaptive',
    deployment: 'claude-sonnet-demo',
    extra: { publisher: 'Anthropic', model_name: 'claude-sonnet-demo', context_window: 200000 },
  })
  await api.claudeRates(claudeId)
  const judgeId = await api.profile({
    endpointId,
    name: 'GPT-4.1 judge',
    apiStyle: 'openai_responses',
    preset: 'openai-standard',
    deployment: 'gpt-4.1-judge',
    meterTerms: 'gpt-4.1',
    extra: { publisher: 'OpenAI', model_name: 'gpt-4.1', context_window: 1000000 },
  })
  await api.refreshPrices()
  await api.call('PUT', `/api/profiles/${judgeId}/rates/output`, { tier: 0, mode: 'azure_auto' })

  // Compare page with three candidates.
  await page.goto('/compare')
  await page.getByLabel('System instructions (optional)').fill('You are a concise assistant for a software team.')
  await page.getByLabel('User prompt').fill('Explain the difference between optimistic and pessimistic locking, with one example each.')
  await chooseProfile(page, 0, 'GPT-5.6 terra')
  const first = candidateCard(page, 0)
  await first.getByLabel('Reasoning effort').selectOption('low')
  await first.getByLabel(/Max output tokens/).fill('2000')
  await first.getByRole('button', { name: 'Duplicate' }).click()
  await candidateCard(page, 1).locator('summary', { hasText: 'Generation settings' }).click()
  await candidateCard(page, 1).getByLabel('Reasoning effort').selectOption('high')
  await page.getByRole('button', { name: 'Add candidate' }).click()
  await chooseProfile(page, 2, 'Claude Sonnet')
  await page.getByLabel('Run title (optional)').fill('Locking explanation')
  await expect(page.getByText('3 calls')).toBeVisible()
  await shot(page, 'compare')

  await page.getByRole('button', { name: 'Run comparison' }).click()
  await expect(page.locator('.page-header').getByText('Completed', { exact: true })).toBeVisible({ timeout: 30_000 })
  const runId = page.url().split('/runs/')[1]
  await page.getByTestId('response-cards').getByRole('article').first().locator('summary', { hasText: 'Cost breakdown' }).click()
  await shot(page, 'run')

  // Judge the run.
  await api.call(
    'POST',
    `/api/runs/${runId}/evaluations`,
    { judge_profile_id: judgeId, settings: { max_output_tokens: 3000 } },
    { 'Idempotency-Key': crypto.randomUUID() },
  )
  await api.waitForRun(runId)
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
  const suite = await api.call<{ id: string }>('POST', '/api/suites', {
    name: 'Support answers',
    description: 'Typical customer questions, three trials each.',
    trials: 3,
    prompts: [
      { title: 'Refund policy', user: 'A customer asks how to get a refund for an annual plan. Answer in three sentences.' },
      { title: 'Password reset', user: 'Explain how to reset a forgotten password, step by step.' },
    ],
    candidates: [
      { profile_id: terraId, settings: { effort: 'low', max_output_tokens: 1500 } },
      { profile_id: claudeId, settings: { max_output_tokens: 1500 } },
    ],
    judge: { enabled: true, judge_profile_id: judgeId, settings: { max_output_tokens: 3000 } },
  })
  for (let i = 0; i < 2; i++) {
    const created = await api.call<{ run_id: string }>('POST', `/api/suites/${suite.id}/runs`, {}, {
      'Idempotency-Key': crypto.randomUUID(),
    })
    await api.waitForRun(created.run_id)
  }
  await page.goto(`/suites/${suite.id}`)
  await expect(page.getByTestId('coverage')).toContainText('of 6 cells') // the latest run is selected first
  await page.getByRole('region', { name: 'Suite runs' }).getByRole('checkbox').nth(1).check()
  await expect(page.getByRole('region', { name: 'Comparison of selected runs' })).toContainText('2 runs')
  await shot(page, 'suite')

  // Rates and projections.
  await page.goto(`/models/pricing/${terraId}`)
  await page.getByRole('button', { name: 'Match Azure prices' }).click()
  await expect(page.getByText('Match results')).toBeVisible()
  await shot(page, 'rates')
  await page.goto('/models/projections')
  await page.getByLabel('Requests per month').fill('250000')
  await page.getByLabel('Output length multiplier').fill('1.5')
  await expect(page.getByRole('region', { name: 'Projection' })).toBeVisible()
  await shot(page, 'projections')
})
