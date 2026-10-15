import { expect, test, type Page } from '@playwright/test'

import { AppApi, mockStats, selectByText, unique, watchConsole } from './helpers'

interface Setup {
  runId: string
  terra: string
  judge: string
  malformed: string
  tiny: string
}

async function setup(api: AppApi): Promise<Setup> {
  const endpointId = await api.endpoint(unique('Judge endpoint'))
  const names = { terra: unique('Terra cand'), judge: unique('Judge good'), malformed: unique('Judge bad'), tiny: unique('Judge tiny') }
  const terraId = await api.profile({
    endpointId,
    name: names.terra,
    apiStyle: 'openai_responses',
    preset: 'openai-reasoning-cache-write',
    deployment: 'gpt-5.6-terra',
  })
  for (const [name, deployment, contextWindow] of [
    [names.judge, 'gpt-4.1-judge', 100000],
    [names.malformed, 'judge-malformed', 100000],
    [names.tiny, 'gpt-4.1-judge-small', 300],
  ] as const) {
    await api.profile({
      endpointId,
      name,
      apiStyle: 'openai_responses',
      preset: 'openai-standard',
      deployment,
      extra: { context_window: contextWindow, model_name: 'gpt-4.1' },
    })
  }
  const runId = await api.run({
    title: unique('Judged run'),
    prompts: [{ system: 'Answer briefly.', user: 'Explain rainbows to a child.' }],
    candidates: [
      { profile_id: terraId, settings: { effort: 'low' } },
      { profile_id: terraId, settings: { effort: 'high' } },
    ],
  })
  return { runId, ...names }
}

async function chooseJudge(page: Page, name: string) {
  await selectByText(page.getByRole('region', { name: 'Judge' }).getByLabel('Judge model profile'), `${name} —`)
}

test('the judge scores anonymized responses; malformed and oversized results stay inspectable', async ({ page, request }) => {
  const problems = watchConsole(page)
  const api = new AppApi(request)
  const { runId, terra, judge, malformed, tiny } = await setup(api)
  await page.goto(`/scoring/${runId}`)
  const panel = page.getByRole('region', { name: 'Judge' })

  await chooseJudge(page, judge)
  await expect(panel.getByRole('table', { name: 'Judge input size check' })).toContainText('fits')
  await panel.getByRole('button', { name: 'Run judge' }).click()
  const card = panel.getByTestId('evaluation').first()
  await expect(card.getByText('Succeeded', { exact: true })).toBeVisible()
  await expect(card.getByTestId('evaluation-row-A')).toBeVisible()
  await expect(card.getByTestId('evaluation-row-B')).toBeVisible()
  await expect(card).toContainText(`${terra} (effort=low)`)
  await expect(card).toContainText(`${terra} (effort=high)`)
  await expect(card).toContainText(/Judge preference: (A|B|tie)/)
  await expect(card).toContainText('Estimated token cost (judging)')
  await expect(page.getByTestId('judge-score')).toHaveCount(2)

  await card.getByRole('button', { name: 'Judge request' }).click()
  const dialog = page.getByRole('dialog', { name: 'Exact judge request' })
  await expect(dialog).toContainText('Explain rainbows to a child.')
  await expect(dialog).toContainText('=== Response A (begin')
  await expect(dialog).not.toContainText(terra)
  await expect(dialog).not.toContainText('effort=')
  await expect(dialog).not.toContainText('gpt-5.6-terra')
  await dialog.getByRole('button', { name: 'Close' }).click()

  await chooseJudge(page, malformed)
  await panel.getByRole('button', { name: 'Run judge' }).click()
  const bad = panel.getByTestId('evaluation').first()
  await expect(bad.getByText('Malformed output', { exact: true })).toBeVisible()
  await expect(bad).toContainText('Problems with the judge output')
  await expect(bad).toContainText('not valid JSON')
  await expect(bad).toContainText('cannot provide JSON')

  await chooseJudge(page, tiny)
  // A 300-token context window: leave room for 100 output tokens.
  await panel.getByText('Judge settings').click()
  await panel.getByLabel('Max output tokens').fill('100')
  await expect(panel.getByRole('table', { name: 'Judge input size check' })).toContainText('too large: will be rejected')
  const before = await mockStats(request)
  await panel.getByRole('button', { name: 'Run judge' }).click()
  const rejected = panel.getByTestId('evaluation').first()
  await expect(rejected.getByText('Rejected (too large)', { exact: true })).toBeVisible()
  await expect(rejected).toContainText('Nothing was sent and nothing was truncated')
  const after = await mockStats(request)
  expect(after.requests['/openai/v1/responses']).toBe(before.requests['/openai/v1/responses'])

  // Earlier evaluations are kept; the newest is shown first.
  await expect(panel.getByTestId('evaluation')).toHaveCount(3)
  expect(problems).toEqual([])
})

test('a judge that also produced responses is flagged', async ({ page, request }) => {
  const api = new AppApi(request)
  const { runId, terra } = await setup(api)
  await page.goto(`/scoring/${runId}`)
  await chooseJudge(page, terra)
  await expect(page.getByRole('region', { name: 'Judge' })).toContainText('The judge also took part in this run')
})

test('rubrics are validated and edited as new versions', async ({ page }) => {
  await page.goto('/rubrics')
  await expect(page.getByTestId('rubric').filter({ hasText: 'Default rubric' })).toContainText('Instruction following')
  await page.getByRole('button', { name: 'New rubric' }).click()
  const form = page.getByRole('form', { name: 'New rubric' })
  const name = unique('Support rubric')
  await form.getByLabel('Rubric name').fill(name)
  await form.getByLabel('Criterion 2 key').fill('instruction_following')
  await expect(form.getByRole('alert')).toContainText('is used twice')
  await expect(form.getByRole('button', { name: 'Save' })).toBeDisabled()
  await form.getByLabel('Criterion 2 key').fill('empathy')
  await form.getByLabel('Criterion 2 name').fill('Empathy')
  await form.getByLabel('Criterion 2 weight').fill('0')
  await expect(form.getByRole('alert')).toContainText('greater than zero')
  await form.getByLabel('Criterion 2 weight').fill('2')
  await form.getByRole('button', { name: 'Save' }).click()
  const rubric = page.getByTestId('rubric').filter({ hasText: name })
  await expect(rubric).toContainText('Version 1 of 1')
  await expect(rubric).toContainText('Empathy')

  await rubric.getByRole('button', { name: 'New version' }).click()
  const edit = rubric.getByRole('form', { name: `New version of ${name}` })
  await edit.getByLabel('Highest score').fill('10')
  await edit.getByRole('button', { name: 'Save' }).click()
  await expect(rubric).toContainText('Version 2 of 2')
  await expect(rubric).toContainText('scores 1–10')
})
