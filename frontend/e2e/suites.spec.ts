import { expect, test } from '@playwright/test'

import { AppApi, selectByText, unique, watchConsole } from './helpers'

test('a suite with repeated trials, automatic judging and a comparison with coverage', async ({ page, request }) => {
  const problems = watchConsole(page)
  const api = new AppApi(request)
  const endpointId = await api.endpoint(unique('Suite endpoint'))
  const terra = unique('Terra suite')
  const judge = unique('Judge')
  await api.profile({
    endpointId,
    name: terra,
    apiStyle: 'openai_responses',
    preset: 'openai-reasoning-cache-write',
    deployment: 'gpt-5.6-terra',
    meterTerms: '5.6 terra',
    longContextThreshold: 272000,
  })
  await api.profile({
    endpointId,
    name: judge,
    apiStyle: 'openai_responses',
    preset: 'openai-standard',
    deployment: 'gpt-4.1-judge',
    extra: { context_window: 100000, model_name: 'gpt-4.1' },
  })
  await api.refreshPrices()

  const suiteName = unique('Sky suite')
  await page.goto('/suites')
  await page.getByRole('button', { name: 'New suite' }).click()
  const form = page.getByRole('form', { name: 'New suite' })
  await form.getByLabel('Suite name').fill(suiteName)
  await form.getByLabel('Trials per prompt').fill('2')
  await form.getByLabel('Prompt 1 user prompt').fill('Why is the sky blue?')
  await form.getByRole('button', { name: 'Add prompt' }).click()
  await form.getByLabel('Prompt 2 title (optional)').fill('Sea')
  await form.getByLabel('Prompt 2 user prompt').fill('Why is the sea salty?')

  await selectByText(form.getByLabel('Model profile for suite candidate 1'), `${terra} —`)
  const first = form.getByTestId('suite-candidate').nth(0)
  await first.getByLabel('Reasoning effort').selectOption('low')
  await first.getByRole('button', { name: 'Duplicate' }).click()
  const second = form.getByTestId('suite-candidate').nth(1)
  await second.getByLabel('Reasoning effort').selectOption('high')

  await form.getByLabel(/Judge every run of this suite automatically/).check()
  await selectByText(form.getByLabel('Judge model profile'), `${judge} —`)
  await form.getByRole('button', { name: 'Save suite' }).click()
  await expect(page).toHaveURL(/\/suites\/[\w-]+$/)
  await expect(page.getByRole('heading', { name: suiteName })).toBeVisible()
  await expect(page.getByText('2 prompts · 2 candidates · 2 trials · automatic judging on')).toBeVisible()

  const runPanel = page.getByRole('region', { name: 'Run suite' })
  await expect(runPanel.getByText('8 calls')).toBeVisible()
  await expect(runPanel.getByText(/plus automatic judging/)).toBeVisible()
  await runPanel.getByRole('button', { name: 'Run suite' }).click()
  await expect(page).toHaveURL(/\/runs\//)
  await expect(page.locator('.page-header').getByText('Completed', { exact: true })).toBeVisible({ timeout: 45_000 })
  await expect(page.getByTestId('evaluation-counts')).toHaveText('Judge evaluations: 4 succeeded', { timeout: 45_000 })
  await expect(page.getByTestId('response-cards').getByTestId('judge-score')).toHaveCount(2)
  await expect(page.locator('.total-box').filter({ hasText: 'Judging' })).not.toContainText('nothing yet')

  await page.getByRole('link', { name: 'Suites & history' }).click()
  await page.getByRole('link', { name: 'Suites', exact: true }).click()
  await page.getByRole('link', { name: suiteName }).click()
  const comparison = page.getByRole('region', { name: 'Comparison of selected runs' })
  await expect(comparison.getByTestId('coverage')).toContainText('4 of 4 cells')
  const rows = comparison.getByRole('row').filter({ hasText: terra })
  await expect(rows).toHaveCount(2)
  for (const label of [`${terra} (effort=low)`, `${terra} (effort=high)`]) {
    const row = comparison.getByRole('row').filter({ hasText: label })
    await expect(row).toContainText('4 judged')
    await expect(row).toContainText('4 comparisons')
    await expect(row).toContainText('0 of 4')
  }

  // Editing keeps the suite; runs keep their own copies of the prompts.
  await page.getByRole('button', { name: 'Edit suite' }).click()
  await page.getByLabel('Suite name').fill(`${suiteName} v2`)
  await page.getByRole('button', { name: 'Save suite' }).click()
  await expect(page.getByRole('heading', { name: `${suiteName} v2` })).toBeVisible()
  await expect(page.getByRole('region', { name: 'Suite runs' }).getByRole('row')).toHaveCount(2)
  expect(problems).toEqual([])
})

test('the compare page can load a saved suite prompt', async ({ page, request }) => {
  const api = new AppApi(request)
  const endpointId = await api.endpoint(unique('Load endpoint'))
  const profileId = await api.profile({
    endpointId,
    name: unique('Loader'),
    apiStyle: 'openai_chat',
    preset: 'generic-chat',
    deployment: 'deepseek-test',
  })
  const name = unique('Library')
  await api.call('POST', '/api/suites', {
    name,
    prompts: [{ title: 'Greeting', system: 'Be formal.', user: 'Say good morning.' }],
    candidates: [{ profile_id: profileId }],
  })
  await page.goto('/compare')
  await selectByText(page.getByLabel('Load from suite'), `${name}: Greeting`)
  await expect(page.getByLabel('System instructions (optional)')).toHaveValue('Be formal.')
  await expect(page.getByLabel('User prompt')).toHaveValue('Say good morning.')
})
