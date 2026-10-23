import { createHash } from 'node:crypto'

import { expect, test } from '@playwright/test'

import {
  AppApi,
  CANARY,
  browserStorage,
  createEndpoint,
  createProfile,
  mockStats,
  rateRow,
  setManualRate,
  unique,
  watchConsole,
} from './helpers'

test.describe('Models and pricing', () => {
  test('API keys are write-only: stored, verified, never shown again', async ({ page, request }) => {
    const problems = watchConsole(page)
    const name = unique('Endpoint')
    await createEndpoint(page, name)
    const card = page.getByTestId('endpoint').filter({ hasText: name })

    await card.getByRole('button', { name: 'Verify (free)' }).click()
    await expect(card.getByText('The key was accepted (models listed).')).toBeVisible()
    const stats = await mockStats(request)
    expect(stats.key_hashes).toContain(createHash('sha256').update(CANARY).digest('hex').slice(0, 16))

    await page.reload()
    await expect(card.getByText('key stored')).toBeVisible()
    await expect(card.getByLabel(`API key for ${name}`)).toHaveValue('')
    await expect(card.getByRole('button', { name: 'Replace key' })).toBeVisible()
    expect(await page.content()).not.toContain(CANARY)
    expect(await browserStorage(page)).not.toContain(CANARY)
    const api = new AppApi(request)
    expect(await api.text('/api/endpoints')).not.toContain(CANARY)

    await card.getByRole('button', { name: 'Remove key' }).click()
    await expect(card.getByText('no key')).toBeVisible()
    expect(problems).toEqual([])
  })

  test('Azure prices match only unambiguous meters; tiers come from ShortCo/LongCo pairs', async ({ page }) => {
    const problems = watchConsole(page)
    const endpoint = unique('Pricing endpoint')
    await createEndpoint(page, endpoint)
    const terra = unique('Terra')
    await createProfile(page, {
      endpoint,
      name: terra,
      api: 'OpenAI Responses',
      preset: 'OpenAI reasoning with cache-write charges (GPT-5.6 and later)',
      deployment: 'gpt-5.6-terra',
      meterTerms: '5.6 terra',
      longContextThreshold: '272000',
    })
    await page.getByRole('row').filter({ hasText: terra }).getByRole('link', { name: 'Rates' }).click()
    await expect(page).toHaveURL(/\/models\/pricing\//)

    await page.getByRole('button', { name: 'Refresh Azure prices' }).click()
    await expect(page.getByText(/eastus2: succeeded \(\d+ price items\)/)).toBeVisible()
    await page.getByRole('button', { name: 'Match Azure prices' }).click()
    await expect(page.getByText('Match results')).toBeVisible()

    // Base tier (ShortCo, Standard, Global) — never the Priority, Data Zone, Batch or "luna" meters.
    await expect(rateRow(page, 'input_uncached')).toContainText('$2.5000')
    await expect(rateRow(page, 'input_uncached')).toContainText('5.6 terra ShortCo Inp Std Gl')
    await expect(rateRow(page, 'cache_read')).toContainText('$0.2500')
    await expect(rateRow(page, 'cache_write')).toContainText('$3.1250')
    await expect(rateRow(page, 'output')).toContainText('$15.0000')
    await expect(rateRow(page, 'output')).toContainText('Azure (auto)')
    // Long-context tier.
    await expect(page.getByRole('heading', { name: 'Long-context tier (above 272000 input tokens)' })).toBeVisible()
    await expect(rateRow(page, 'input_uncached', 272000)).toContainText('$5.0000')
    await expect(rateRow(page, 'output', 272000)).toContainText('$22.5000')
    expect(problems).toEqual([])
  })

  test('future-dated, unit-conflicting and ambiguous meters are never applied automatically', async ({ page }) => {
    const endpoint = unique('Pin endpoint')
    await createEndpoint(page, endpoint)
    const gpt41 = unique('GPT-4.1')
    await createProfile(page, {
      endpoint,
      name: gpt41,
      api: 'OpenAI Responses',
      preset: 'OpenAI standard (GPT-4.1, GPT-4o)',
      deployment: 'gpt-4.1',
      meterTerms: 'gpt-4.1',
    })
    await page.getByRole('row').filter({ hasText: gpt41 }).getByRole('link', { name: 'Rates' }).click()
    await page.getByRole('button', { name: 'Refresh Azure prices' }).click()
    await expect(page.getByText(/eastus2: succeeded/)).toBeVisible()
    await page.getByRole('button', { name: 'Match Azure prices' }).click()
    await expect(page.getByText('Match results')).toBeVisible()

    await expect(rateRow(page, 'input_uncached')).toContainText('$2.0000')
    // The same output meter has a price change scheduled for the future: today's price applies.
    await expect(rateRow(page, 'output')).toContainText('$8.0000')
    await expect(rateRow(page, 'output')).not.toContainText('$7.0000')
    // One cached-input meter's name disagrees with its unit and the other has no deployment type.
    await expect(rateRow(page, 'cache_read')).toContainText('unknown')

    await rateRow(page, 'cache_read').getByRole('button', { name: 'Pin meter' }).click()
    const dialog = page.getByRole('dialog')
    await dialog.getByLabel('Search the cached Azure catalog').fill('gpt-4.1 cached')
    const results = dialog.getByRole('table', { name: 'Catalog search results' })
    const meter = results.getByRole('row').filter({ hasText: 'gpt-4.1 cached Inp glbl 1M Tokens' })
    await meter.getByRole('button', { name: 'Pin' }).click()
    await expect(dialog).toBeHidden()
    await expect(rateRow(page, 'cache_read')).toContainText('Azure (pinned)')
    // Priced per 1K in the catalog, shown per 1M.
    await expect(rateRow(page, 'cache_read')).toContainText('$0.5000')
    await expect(rateRow(page, 'cache_read')).toContainText('Quoted as $0.000500 per 1K')

    // Two products publish the same meter name: ambiguous, so the user picks one.
    const dup = unique('Dup')
    await createProfile(page, {
      endpoint,
      name: dup,
      api: 'OpenAI Responses',
      preset: 'OpenAI standard (GPT-4.1, GPT-4o)',
      deployment: 'dup-model',
      meterTerms: 'dup-model',
    })
    await page.getByRole('row').filter({ hasText: dup }).getByRole('link', { name: 'Rates' }).click()
    await page.getByRole('button', { name: 'Match Azure prices' }).click()
    await expect(page.getByText('Match results')).toBeVisible()
    await expect(page.getByText(/Uncached input: ambiguous/)).toBeVisible()
    await expect(rateRow(page, 'input_uncached')).toContainText('unknown')
    await rateRow(page, 'input_uncached').getByRole('button', { name: 'Pin meter' }).click()
    await expect(dialog.getByText('Candidates from the last match')).toBeVisible()
    await dialog
      .getByRole('table', { name: 'Candidates from the last match' })
      .getByRole('row')
      .filter({ hasText: '$1.000000' })
      .first()
      .getByRole('button', { name: 'Pin' })
      .click()
    await expect(dialog).toBeHidden()
    await expect(rateRow(page, 'input_uncached')).toContainText('$1.0000')
    await expect(rateRow(page, 'input_uncached')).toContainText('Azure (pinned)')
  })

  test('manual rates need a source URL; overrides are labelled custom', async ({ page }) => {
    const endpoint = unique('Claude endpoint')
    await createEndpoint(page, endpoint)
    const claude = unique('Claude')
    await createProfile(page, {
      endpoint,
      name: claude,
      api: 'Claude Messages',
      preset: 'Claude adaptive thinking (Claude 4.6 and later, Claude 5 family)',
      deployment: 'claude-opus-test',
    })
    await page.getByRole('row').filter({ hasText: claude }).getByRole('link', { name: 'Rates' }).click()
    await expect(rateRow(page, 'output')).toContainText('unknown')

    await rateRow(page, 'output').getByRole('button', { name: 'Manual' }).click()
    const dialog = page.getByRole('dialog')
    await dialog.getByLabel('Price (USD)').fill('25')
    await expect(dialog.getByRole('button', { name: 'Save' })).toBeDisabled()
    await dialog.getByLabel('Source URL (required)').fill('https://www.anthropic.com/pricing')
    await dialog.getByRole('button', { name: 'Save' }).click()
    await expect(dialog).toBeHidden()
    await expect(rateRow(page, 'output')).toContainText('$25.0000')
    await expect(rateRow(page, 'output')).toContainText('Manual')
    await expect(rateRow(page, 'output').getByRole('link', { name: 'Source' })).toHaveAttribute(
      'href',
      'https://www.anthropic.com/pricing',
    )

    await setManualRate(page, 'input_uncached', '5')
    await setManualRate(page, 'cache_read', '0.50')
    await setManualRate(page, 'cache_write_5m', '6.25')
    await setManualRate(page, 'cache_write_1h', '10')
    await expect(rateRow(page, 'cache_write_1h')).toContainText('$10.0000')

    await rateRow(page, 'output').getByRole('button', { name: 'Override' }).click()
    await dialog.getByLabel('Price (USD)').fill('20')
    await expect(dialog.getByRole('button', { name: 'Save' })).toBeDisabled()
    await dialog.getByLabel('Label / reason (required)').fill('Negotiated discount')
    await dialog.getByRole('button', { name: 'Save' }).click()
    await expect(dialog).toBeHidden()
    await expect(rateRow(page, 'output')).toContainText('$20.0000')
    await expect(rateRow(page, 'output')).toContainText('Custom rate: Negotiated discount')

    await rateRow(page, 'output').getByRole('button', { name: 'Clear override' }).click()
    await expect(rateRow(page, 'output')).toContainText('$25.0000')
    await expect(rateRow(page, 'output')).not.toContainText('Custom rate')
  })

  test('manual and override prices use the configured currency in later estimates', async ({ page, request }) => {
    const api = new AppApi(request)
    const settings = await api.call<{ settings: Record<string, unknown> }>('GET', '/api/settings')
    try {
      await api.call('PUT', '/api/settings', { ...settings.settings, currency: 'EUR' })
      const endpointId = await api.endpoint(unique('Currency endpoint'))
      const profileId = await api.profile({
        endpointId,
        name: unique('Currency profile'),
        apiStyle: 'openai_chat',
        preset: 'generic-chat',
        deployment: 'currency-chat',
      })

      await page.goto(`/models/pricing/${profileId}`)
      const inputRow = rateRow(page, 'input_uncached')
      const outputRow = rateRow(page, 'output')
      await inputRow.getByRole('button', { name: 'Manual' }).click()
      let dialog = page.getByRole('dialog')
      await expect(dialog.getByLabel('Price (EUR)')).toBeVisible()
      await dialog.getByLabel('Price (EUR)').fill('2')
      await dialog.getByLabel('Source URL (required)').fill('https://example.test/pricing')
      await dialog.getByRole('button', { name: 'Save' }).click()
      await expect(dialog).toBeHidden()
      await expect(inputRow).toContainText('EUR 2.0000')

      await outputRow.getByRole('button', { name: 'Manual' }).click()
      dialog = page.getByRole('dialog')
      await dialog.getByLabel('Price (EUR)').fill('25')
      await dialog.getByLabel('Source URL (required)').fill('https://example.test/pricing')
      await dialog.getByRole('button', { name: 'Save' }).click()
      await expect(dialog).toBeHidden()
      await expect(outputRow).toContainText('EUR 25.0000')

      await outputRow.getByRole('button', { name: 'Override' }).click()
      dialog = page.getByRole('dialog')
      await expect(dialog.getByLabel('Price (EUR)')).toBeVisible()
      await dialog.getByLabel('Price (EUR)').fill('20')
      await dialog.getByLabel('Label / reason (required)').fill('Negotiated rate')
      await dialog.getByRole('button', { name: 'Save' }).click()
      await expect(dialog).toBeHidden()
      await expect(outputRow).toContainText('EUR 20.0000')

      type StoredRates = {
        currency: string
        card: {
          tiers: Array<{
            above_input_tokens: number
            categories: Record<string, { rate?: { price: string; currency: string; source: string } }>
          }>
        }
      }
      const stored = await api.call<StoredRates>('GET', `/api/profiles/${profileId}/rates`)
      const base = stored.card.tiers.find((tier) => tier.above_input_tokens === 0)
      expect(stored.currency).toBe('EUR')
      expect(base?.categories.input_uncached.rate).toMatchObject({ price: '2', currency: 'EUR', source: 'manual' })
      expect(base?.categories.output.rate).toMatchObject({ price: '20', currency: 'EUR', source: 'custom_override' })

      const runId = await api.run({
        title: unique('Currency estimate'),
        prompts: [{ user: 'Say hello.' }],
        candidates: [{ profile_id: profileId }],
      })
      const run = await api.call<{ generation_cost: { status: string; currency: string; total: string | null } }>(
        'GET',
        `/api/runs/${runId}`,
      )
      expect(run.generation_cost.status).toBe('complete')
      expect(run.generation_cost.currency).toBe('EUR')
      expect(Number(run.generation_cost.total)).toBeGreaterThan(0)
    } finally {
      await api.call('PUT', '/api/settings', settings.settings)
    }
  })
})
