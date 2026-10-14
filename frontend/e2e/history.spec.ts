import { expect, test } from '@playwright/test'

import { AppApi, candidateCard, chooseProfile, unique, watchConsole } from './helpers'

test.describe('History and manual scoring', () => {
  test('a run keeps the prices it was created with after rates change', async ({ page, request }) => {
    const problems = watchConsole(page)
    const api = new AppApi(request)
    const endpointId = await api.endpoint(unique('History endpoint'))
    const claude = unique('Claude history')
    const claudeId = await api.profile({
      endpointId,
      name: claude,
      apiStyle: 'anthropic_messages',
      preset: 'claude-adaptive',
      deployment: 'claude-haiku-test',
    })
    await api.claudeRates(claudeId)

    const title = unique('Price history')
    await page.goto('/compare')
    await page.getByLabel('User prompt').fill('Name three prime numbers.')
    await chooseProfile(page, 0, claude)
    await candidateCard(page, 0).getByLabel(/Max output tokens/).fill('300')
    await page.getByLabel('Run title (optional)').fill(title)
    await page.getByRole('button', { name: 'Run comparison' }).click()
    await expect(page).toHaveURL(/\/runs\//)
    const runId = page.url().split('/runs/')[1]

    const card = page.getByRole('article', { name: `Response from ${claude}`, exact: true })
    await expect(card.getByText('Succeeded', { exact: true })).toBeVisible()
    const cost = card.locator('.cost')
    await expect(cost).not.toContainText('incomplete')
    const originalCost = ((await cost.textContent()) ?? '').trim()
    expect(originalCost).toMatch(/^\$\d/)
    const generation = page.locator('.total-box').filter({ hasText: 'Generation' }).locator('.value')
    await expect(generation).toHaveText(originalCost)
    await expect(page.locator('.total-box').filter({ hasText: 'Judging' }).locator('.value')).toHaveText('$0.00')

    // Rates go up tenfold after the run.
    await api.claudeRates(claudeId, { input: '30', read: '3', w5m: '37.5', w1h: '60', output: '150' })
    await page.reload()
    await expect(card.locator('.cost')).toHaveText(originalCost)
    await expect(generation).toHaveText(originalCost)

    await page.goto('/history')
    await expect(page.getByRole('row').filter({ hasText: title })).toContainText(originalCost)

    // A new run of the same prompt is priced with the new rates.
    const created = await api.call<{ run_id: string }>(
      'POST',
      '/api/runs',
      {
        kind: 'compare',
        title: `${title} (new prices)`,
        prompts: [{ user: 'Name three prime numbers.' }],
        candidates: [{ profile_id: claudeId, settings: { max_output_tokens: 300 } }],
      },
      { 'Idempotency-Key': crypto.randomUUID() },
    )
    await api.waitForRun(created.run_id)
    const fresh = await api.call<{ generation_cost: { total: string } }>('GET', `/api/runs/${created.run_id}`)
    const original = await api.call<{ generation_cost: { total: string } }>('GET', `/api/runs/${runId}`)
    expect(Number(fresh.generation_cost.total)).toBeGreaterThan(Number(original.generation_cost.total))
    expect(problems).toEqual([])
  })

  test('manual scores and notes are saved per response and shown on the Scoring page', async ({ page, request }) => {
    const api = new AppApi(request)
    const endpointId = await api.endpoint(unique('Scoring endpoint'))
    const name = unique('Chat model')
    const profileId = await api.profile({
      endpointId,
      name,
      apiStyle: 'openai_chat',
      preset: 'generic-chat',
      deployment: 'deepseek-test',
    })
    const created = await api.call<{ run_id: string }>(
      'POST',
      '/api/runs',
      { kind: 'compare', title: unique('Scoring run'), prompts: [{ user: 'Hi' }], candidates: [{ profile_id: profileId }] },
      { 'Idempotency-Key': crypto.randomUUID() },
    )
    await api.waitForRun(created.run_id)

    await page.goto(`/runs/${created.run_id}`)
    const card = page.getByRole('article', { name: `Response from ${name}`, exact: true })
    await card.getByRole('button', { name: 'Score 4' }).click()
    await expect(card.getByRole('button', { name: 'Score 4' })).toHaveAttribute('aria-pressed', 'true')
    const notes = card.getByLabel('Manual score notes')
    await notes.fill('Correct and concise.')
    await notes.blur()
    await expect
      .poll(async () => {
        const detail = await api.call<{ manual_scores: Record<string, { notes: string }> }>('GET', `/api/runs/${created.run_id}`)
        return Object.values(detail.manual_scores)[0]?.notes
      })
      .toBe('Correct and concise.')

    await page.reload()
    await expect(card.getByRole('button', { name: 'Score 4' })).toHaveAttribute('aria-pressed', 'true')
    await expect(card.getByLabel('Manual score notes')).toHaveValue('Correct and concise.')

    await page.getByRole('link', { name: 'Score responses' }).click()
    await expect(page).toHaveURL(new RegExp(`/scoring/${created.run_id}`))
    const scoring = page.getByRole('article', { name: `Score ${name}`, exact: true })
    await expect(scoring.getByRole('button', { name: 'Score 4' })).toHaveAttribute('aria-pressed', 'true')
    await scoring.getByRole('button', { name: 'Score 2' }).click()
    await expect(scoring.getByRole('button', { name: 'Score 2' })).toHaveAttribute('aria-pressed', 'true')
    await scoring.getByRole('button', { name: 'Clear' }).click()
    await expect(scoring.getByRole('button', { name: 'Score 2' })).toHaveAttribute('aria-pressed', 'false')
  })
})
