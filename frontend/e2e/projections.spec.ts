import { expect, test } from '@playwright/test'

import { AppApi, unique, watchConsole } from './helpers'

function dollars(text: string | null): number {
  const match = /\$([\d,]+\.\d+)/.exec(text ?? '')
  if (!match) throw new Error(`no amount in ${text}`)
  return Number(match[1].replaceAll(',', ''))
}

test('projections re-price measured usage under what-if assumptions and either price basis', async ({ page, request }) => {
  const problems = watchConsole(page)
  const api = new AppApi(request)
  const endpointId = await api.endpoint(unique('Projection endpoint'))
  const name = unique('Claude projection')
  const profileId = await api.profile({
    endpointId,
    name,
    apiStyle: 'anthropic_messages',
    preset: 'claude-adaptive',
    deployment: 'claude-projection-test',
  })
  await api.claudeRates(profileId)
  const title = unique('Projection run')
  await api.run({
    title,
    prompts: [{ user: 'Summarize the water cycle.' }],
    candidates: [{ profile_id: profileId, settings: { max_output_tokens: 300 } }],
    trials: 2,
  })

  await page.goto('/models/projections')
  const sources = page.getByRole('group', { name: 'Source runs' })
  for (const box of await sources.getByRole('checkbox').all()) await box.uncheck()
  await sources.getByRole('checkbox', { name: new RegExp(title) }).check()
  await page.getByLabel('Requests per month').fill('50000')
  const projection = page.getByRole('region', { name: 'Projection' })
  await expect(projection.getByRole('heading', { name: 'Estimated token costs (projection)' })).toBeVisible()
  await expect(projection).toContainText('prices: the price snapshot each run was created with')
  const row = projection.getByTestId('projection-row').filter({ hasText: name })
  await expect(row).toBeVisible()
  const cells = row.getByRole('cell')
  await expect(cells.nth(1)).toHaveText('2')
  const perRequest = dollars(await cells.nth(2).textContent())
  const per1k = dollars(await cells.nth(3).textContent())
  expect(per1k).toBeCloseTo(perRequest * 1000, 2)
  await expect(projection.getByText('Per month (50,000 requests)')).toBeVisible()

  // The spread across requests: nearest-rank percentiles, and monthly figures "if every request cost" one of them.
  const spread = projection.getByTestId('projection-spread-row').filter({ hasText: name })
  const spreadCells = spread.getByRole('cell')
  const [p50, p90, p95, max] = await Promise.all([1, 2, 3, 4].map(async (i) => dollars(await spreadCells.nth(i).textContent())))
  expect(p50).toBeLessThanOrEqual(p90)
  expect(p90).toBeLessThanOrEqual(p95)
  expect(p95).toBeLessThanOrEqual(max)
  await expect(projection.getByText('Per month if every request cost the p95', { exact: true })).toBeVisible()
  expect(dollars(await spreadCells.nth(6).textContent())).toBeCloseTo(p95 * 50000, 1)
  await expect(spread).toContainText('nearest-rank over 2 requests with complete estimates (none excluded)')
  await expect(spread).toContainText('Only 2 requests priced: percentiles from fewer than 30 are rough.')

  // Batch needs both values; half the requests at half the price is three quarters of the cost per request.
  await page.getByLabel('Batch share of requests (%)').fill('50')
  await expect(page.getByText('Enter both the batch share and the batch price ratio, or neither.')).toBeVisible()
  await expect(projection).toBeHidden()
  await page.getByLabel('Batch price ratio').fill('0.5')
  await expect(projection).toContainText('batch: 50% of requests at x0.5 the cost (entered ratio, not a published batch price)')
  await expect.poll(async () => dollars(await row.getByRole('cell').nth(2).textContent())).toBeCloseTo(perRequest * 0.75, 5)
  await page.getByLabel('Batch price ratio').fill('1.5')
  await expect(page.getByText('The batch price ratio must be greater than 0 and at most 1.')).toBeVisible()
  await page.getByLabel('Batch share of requests (%)').fill('')
  await page.getByLabel('Batch price ratio').fill('')
  await expect(projection).not.toContainText('batch:')
  await expect.poll(async () => dollars(await row.getByRole('cell').nth(2).textContent())).toBeCloseTo(perRequest, 6)

  await page.getByLabel('Output length multiplier').fill('3')
  await expect(projection).toContainText('output length x3')
  await expect.poll(async () => dollars(await row.getByRole('cell').nth(2).textContent())).toBeGreaterThan(perRequest)

  // Claude cache writes from a share need a TTL when the run does not show one: a labelled assumption.
  const ttl = page.getByLabel('Cache write TTL when unknown')
  await expect(ttl).toBeDisabled()
  await page.getByLabel('Cache write share of input (%)').fill('30')
  await expect(row).toContainText('cache writes priced at the 5-minute cache rate (assumed TTL)')
  const fiveMinute = dollars(await row.getByRole('cell').nth(2).textContent())
  await ttl.selectOption('1h')
  await expect(row).toContainText('cache writes priced at the 1-hour cache rate (assumed TTL)')
  await expect.poll(async () => dollars(await row.getByRole('cell').nth(2).textContent())).toBeGreaterThan(fiveMinute)
  await page.getByLabel('Cache write share of input (%)').fill('')
  await expect(ttl).toBeDisabled()

  // New rates do not change the original basis; the current basis uses them.
  await page.getByLabel('Output length multiplier').fill('1')
  await api.claudeRates(profileId, { input: '30', read: '3', w5m: '37.5', w1h: '60', output: '150' })
  await page.getByLabel('Requests per month').fill('50001')
  await expect(projection.getByText('Per month (50,001 requests)')).toBeVisible()
  await expect.poll(async () => dollars(await row.getByRole('cell').nth(2).textContent())).toBeCloseTo(perRequest, 6)
  await page.getByLabel('Prices', { exact: true }).selectOption('current')
  await expect(projection).toContainText('prices: current rates')
  await expect.poll(async () => dollars(await row.getByRole('cell').nth(2).textContent())).toBeGreaterThan(perRequest * 5)
  expect(problems).toEqual([])
})

test('what-if controls for categories that are not billed separately are disabled', async ({ page, request }) => {
  const api = new AppApi(request)
  const endpointId = await api.endpoint(unique('Standard endpoint'))
  const profileId = await api.profile({
    endpointId,
    name: unique('Standard'),
    apiStyle: 'openai_responses',
    preset: 'openai-standard',
    deployment: 'gpt-4.1',
  })
  for (const [category, price] of [
    ['input_uncached', '2'],
    ['cache_read', '0.5'],
    ['output', '8'],
  ])
    await api.manualRate(profileId, category, price)
  const title = unique('Standard run')
  await api.run({ title, prompts: [{ user: 'Hi' }], candidates: [{ profile_id: profileId }] })
  await page.goto('/models/projections')
  const sources = page.getByRole('group', { name: 'Source runs' })
  for (const box of await sources.getByRole('checkbox').all()) await box.uncheck()
  await sources.getByRole('checkbox', { name: new RegExp(title) }).check()
  await expect(page.getByRole('region', { name: 'Projection' })).toBeVisible()
  await expect(page.getByLabel('Cache write share of input (%)')).toBeDisabled()
  await expect(page.getByLabel('Cache read share of input (%)')).toBeEnabled()
})
