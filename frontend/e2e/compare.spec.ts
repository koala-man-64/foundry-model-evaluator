import { expect, test, type Locator, type Page } from '@playwright/test'

import { AppApi, candidateCard, chooseProfile, mockStats, unique, watchConsole } from './helpers'

interface Setup {
  terra: string
  claude: string
  broken: string
}

async function setup(api: AppApi): Promise<Setup> {
  const endpointId = await api.endpoint(unique('Compare endpoint'))
  const names = { terra: unique('Terra'), claude: unique('Claude'), broken: unique('Broken') }
  await api.profile({
    endpointId,
    name: names.terra,
    apiStyle: 'openai_responses',
    preset: 'openai-reasoning-cache-write',
    deployment: 'gpt-5.6-terra',
    meterTerms: '5.6 terra',
    longContextThreshold: 272000,
  })
  const claudeId = await api.profile({
    endpointId,
    name: names.claude,
    apiStyle: 'anthropic_messages',
    preset: 'claude-adaptive',
    deployment: 'claude-sonnet-test',
  })
  await api.claudeRates(claudeId)
  await api.profile({ endpointId, name: names.broken, apiStyle: 'openai_chat', preset: 'generic-chat', deployment: 'fail-500' })
  await api.refreshPrices()
  return names
}

function metric(card: Locator, term: string) {
  return card.locator('dt', { hasText: term }).locator('xpath=following-sibling::dd[1]')
}

async function openSettings(page: Page, index: number) {
  const card = candidateCard(page, index)
  const details = card.locator('details').first()
  if (!(await details.evaluate((element) => (element as HTMLDetailsElement).open))) {
    await card.locator('summary', { hasText: 'Generation settings' }).click()
  }
  return card
}

test.describe('Compare', () => {
  test('unsupported controls are disabled and stale values block the run instead of being dropped', async ({
    page,
    request,
  }) => {
    const problems = watchConsole(page)
    const { terra, claude, broken } = await setup(new AppApi(request))
    await page.goto('/compare')
    await page.getByLabel('User prompt').fill('Say hello.')

    await chooseProfile(page, 0, terra)
    const first = await openSettings(page, 0)
    await expect(first.getByLabel('Top K')).toBeDisabled()
    await expect(first.getByLabel('Temperature')).toBeDisabled()
    await expect(first.getByLabel('Thinking mode')).toBeDisabled()
    await expect(first.getByLabel('Reasoning effort')).toBeEnabled()

    await chooseProfile(page, 0, claude)
    await expect(first.getByLabel('Verbosity')).toBeDisabled()
    await expect(first.getByLabel('Seed')).toBeDisabled()
    await expect(first.getByLabel('Thinking mode')).toBeEnabled()
    // Claude requires an output limit; switching filled in a default instead of leaving it empty.
    await expect(first.getByLabel(/Max output tokens/)).not.toHaveValue('')

    await chooseProfile(page, 0, terra)
    await first.getByLabel('Reasoning effort').selectOption('high')
    await chooseProfile(page, 0, broken)
    await expect(first.getByText('This value is not supported by the selected profile.')).toBeVisible()
    await expect(page.getByText(/Reasoning effort is set but not supported/)).toBeVisible()
    await expect(page.getByRole('button', { name: 'Run comparison' })).toBeDisabled()

    await first.getByRole('button', { name: 'Clear' }).click()
    await expect(first.getByText('This value is not supported by the selected profile.')).toBeHidden()
    await expect(page.getByText('1 call')).toBeVisible()
    await expect(page.getByRole('button', { name: 'Run comparison' })).toBeEnabled()
    expect(problems).toEqual([])
  })

  test('runs one model at two efforts, a Claude model and a failing deployment side by side', async ({ page, request }) => {
    const problems = watchConsole(page)
    const api = new AppApi(request)
    const { terra, claude, broken } = await setup(api)
    const title = unique('E2E comparison')
    await page.goto('/compare')
    await page.getByLabel('System instructions (optional)').fill('Answer briefly.')
    await page.getByLabel('User prompt').fill('Explain prompt caching in two sentences.')

    await chooseProfile(page, 0, terra)
    const first = await openSettings(page, 0)
    await first.getByLabel('Reasoning effort').selectOption('low')
    await first.getByLabel(/Max output tokens/).fill('4000')
    await first.getByRole('button', { name: 'Duplicate' }).click()
    const second = await openSettings(page, 1)
    await second.getByLabel('Reasoning effort').selectOption('high')

    await page.getByRole('button', { name: 'Add candidate' }).click()
    await chooseProfile(page, 2, claude)
    const third = await openSettings(page, 2)
    await third.getByLabel(/Max output tokens/).fill('600')

    await page.getByRole('button', { name: 'Add candidate' }).click()
    await chooseProfile(page, 3, broken)

    await page.getByLabel('Run title (optional)').fill(title)
    await expect(page.getByText('4 calls')).toBeVisible()
    const runButton = page.getByRole('button', { name: 'Run comparison' })
    await expect(runButton).toBeEnabled()
    const before = await mockStats(request)
    await runButton.click()
    await expect(page).toHaveURL(/\/runs\//)

    await expect(page.getByRole('heading', { name: title })).toBeVisible()
    await expect(page.locator('.page-header').getByText('Completed with errors', { exact: true })).toBeVisible({ timeout: 30_000 })
    await expect(page.getByTestId('response-cards').getByRole('article')).toHaveCount(4)

    const low = page.getByRole('article', { name: `Response from ${terra} (effort=low)`, exact: true })
    const high = page.getByRole('article', { name: `Response from ${terra} (effort=high)`, exact: true })
    const claudeCard = page.getByRole('article', { name: `Response from ${claude}`, exact: true })
    const failed = page.getByRole('article', { name: `Response from ${broken}`, exact: true })

    await expect(low.getByText('Succeeded', { exact: true })).toBeVisible()
    await expect(low.getByTestId('response-text')).toContainText('Mock answer from gpt-5.6-terra (effort: low)')
    await expect(high.getByTestId('response-text')).toContainText('(effort: high)')
    await expect(metric(low, 'Reasoning (in output)')).toHaveText('64')
    await expect(metric(high, 'Reasoning (in output)')).toHaveText('1,024')
    for (const card of [low, high, claudeCard]) {
      await expect(card.getByText('Succeeded', { exact: true })).toBeVisible()
      await expect(card.getByText('Estimated token cost')).toBeVisible()
      await expect(card.locator('.cost')).toContainText('$')
      await expect(card.locator('.cost')).not.toContainText('incomplete')
      await expect(metric(card, 'Total input')).not.toHaveText('not reported')
    }
    // GPT-5.6 reports cache writes; Claude's are split by TTL.
    await expect(metric(claudeCard, 'Cache write')).not.toHaveText('not reported')

    // The failed call was sent but reported no usage: the estimate is incomplete, never $0.
    await expect(failed.getByText('Failed', { exact: true })).toBeVisible()
    await expect(failed.getByText('Usage not reported.')).toBeVisible()
    await expect(failed.locator('.cost')).toContainText('incomplete')
    await expect(page.locator('.total-box').filter({ hasText: 'Generation' })).toContainText('1 of 4 incomplete')

    // The exact request and effective settings are preserved.
    await high.getByRole('button', { name: 'Request' }).click()
    const requestDialog = page.getByRole('dialog', { name: 'Exact request and effective settings' })
    await expect(requestDialog).toContainText('"effort": "high"')
    await expect(requestDialog).toContainText('"store": false')
    await expect(requestDialog).toContainText('Answer briefly.')
    await requestDialog.getByRole('button', { name: 'Close' }).click()

    // An explicit retry is a new attempt; nothing was retried automatically.
    await failed.getByRole('button', { name: 'Retry' }).click()
    await expect(failed.getByText('retry', { exact: true })).toBeVisible()
    await expect(failed.getByText('Failed', { exact: true })).toBeVisible()
    await expect(page.getByText('Explicit retries are separate attempt records')).toBeVisible()
    const after = await mockStats(request)
    const chat = '/openai/v1/chat/completions'
    expect((after.requests[chat] ?? 0) - (before.requests[chat] ?? 0)).toBe(2)

    await page.getByRole('link', { name: 'Suites & history' }).click()
    const row = page.getByRole('row').filter({ hasText: title })
    await expect(row).toContainText('Completed with errors')
    await expect(row).toContainText('3/5')
    await expect(row).toContainText('2 failed')
    expect(problems).toEqual([])
  })

  test('cancelling stops the in-flight call and never sends queued ones', async ({ page, request }) => {
    const api = new AppApi(request)
    const endpointId = await api.endpoint(unique('Cancel endpoint'))
    const slow = unique('Slow')
    await api.profile({ endpointId, name: slow, apiStyle: 'openai_responses', preset: 'openai-standard', deployment: 'slow-30' })
    await api.setConcurrency(1)
    try {
      await page.goto('/compare')
      await page.getByLabel('User prompt').fill('This run will be cancelled.')
      await chooseProfile(page, 0, slow)
      const first = await openSettings(page, 0)
      await first.getByLabel('Temperature').fill('0.2')
      await first.getByRole('button', { name: 'Duplicate' }).click()
      const second = await openSettings(page, 1)
      await second.getByLabel('Temperature').fill('0.8')
      await expect(page.getByText('2 calls')).toBeVisible()
      await page.getByRole('button', { name: 'Run comparison' }).click()
      await expect(page).toHaveURL(/\/runs\//)

      await expect(page.getByText('Waiting for the response…')).toBeVisible()
      await expect(page.getByText('Waiting for a free slot…')).toBeVisible()
      await page.getByRole('button', { name: 'Cancel run' }).click()
      await expect(page.locator('.page-header').getByText('Cancelled', { exact: true })).toBeVisible()

      const inflight = page.getByRole('article', { name: `Response from ${slow} (temperature=0.2)`, exact: true })
      const queued = page.getByRole('article', { name: `Response from ${slow} (temperature=0.8)`, exact: true })
      await expect(inflight.getByText('Cancelled', { exact: true })).toBeVisible()
      await expect(inflight.locator('.cost')).toContainText('incomplete')
      await expect(queued.getByText('Cancelled', { exact: true })).toBeVisible()
      await expect(queued.locator('.cost')).toContainText('$0.00')
      await expect(queued.locator('.cost')).toContainText('not sent')
    } finally {
      await api.setConcurrency(3)
    }
  })

  test('a budget skips calls whose worst case could exceed it', async ({ page, request }) => {
    const api = new AppApi(request)
    const endpointId = await api.endpoint(unique('Budget endpoint'))
    const claude = unique('Claude budget')
    const claudeId = await api.profile({
      endpointId,
      name: claude,
      apiStyle: 'anthropic_messages',
      preset: 'claude-adaptive',
      deployment: 'claude-budget-test',
    })
    await api.claudeRates(claudeId)
    const before = await mockStats(request)
    await page.goto('/compare')
    await page.getByLabel('User prompt').fill('Too expensive for the budget.')
    await chooseProfile(page, 0, claude)
    await page.getByLabel('Budget (optional, USD)').fill('0.000001')
    await expect(page.getByText(/worst-case estimated token cost \$/)).toBeVisible()
    await page.getByRole('button', { name: 'Run comparison' }).click()
    await expect(page).toHaveURL(/\/runs\//)
    const card = page.getByRole('article', { name: `Response from ${claude}`, exact: true })
    await expect(card.getByText('Skipped (budget)', { exact: true })).toBeVisible()
    await expect(card.locator('.cost')).toContainText('not sent')
    const after = await mockStats(request)
    const messages = '/anthropic/v1/messages'
    expect(after.requests[messages] ?? 0).toBe(before.requests[messages] ?? 0)
  })
})
