import { createHash } from 'node:crypto'
import { readFileSync, readdirSync, statSync } from 'node:fs'
import { join } from 'node:path'

import { expect, test } from '@playwright/test'

import { AppApi, CANARY, browserStorage, chooseProfile, createEndpoint, mockStats, unique } from './helpers'

function filesUnder(dir: string): string[] {
  return readdirSync(dir).flatMap((name) => {
    const path = join(dir, name)
    return statSync(path).isDirectory() ? filesUnder(path) : [path]
  })
}

test('the API key never reaches the page, browser storage, API responses, the database or logs', async ({
  page,
  request,
}) => {
  const api = new AppApi(request)
  const endpointName = unique('Secret endpoint')
  await createEndpoint(page, endpointName) // the key is typed into the browser once
  const endpoints = await api.call<{ id: string; name: string }[]>('GET', '/api/endpoints')
  const endpointId = endpoints.find((e) => e.name === endpointName)?.id ?? ''
  const profile = unique('Secret profile')
  await api.profile({ endpointId, name: profile, apiStyle: 'openai_chat', preset: 'generic-chat', deployment: 'rate-limited' })

  await page.goto('/compare')
  await page.getByLabel('User prompt').fill('hello')
  await chooseProfile(page, 0, profile)
  await page.getByRole('button', { name: 'Run comparison' }).click()
  await expect(page).toHaveURL(/\/runs\//)
  const runId = page.url().split('/runs/')[1]
  const card = page.getByRole('article', { name: `Response from ${profile}`, exact: true })
  await expect(card.getByText('Failed', { exact: true })).toBeVisible()
  await expect(card).toContainText('429')

  // The key was really sent to the provider (the mock records only a hash prefix).
  const hash = createHash('sha256').update(CANARY).digest('hex').slice(0, 16)
  expect((await mockStats(request)).key_hashes).toContain(hash)

  await card.getByRole('button', { name: 'Request' }).click()
  await expect(page.getByRole('dialog', { name: 'Exact request and effective settings' })).toBeVisible()
  expect(await page.content()).not.toContain(CANARY)
  await page.getByRole('dialog', { name: 'Exact request and effective settings' }).getByRole('button', { name: 'Close' }).click()
  await card.getByRole('button', { name: 'Raw response' }).click()
  await expect(page.getByRole('dialog', { name: 'Raw provider response' })).toBeVisible()
  expect(await page.content()).not.toContain(CANARY)
  expect(await browserStorage(page)).not.toContain(CANARY)

  const detail = await api.call<{ attempts: { id: string }[] }>('GET', `/api/runs/${runId}`)
  const paths = [
    '/api/endpoints',
    '/api/profiles',
    '/api/settings',
    '/api/runs',
    `/api/runs/${runId}`,
    ...detail.attempts.map((a) => `/api/attempts/${a.id}/raw`),
  ]
  for (const path of paths) expect(await api.text(path), path).not.toContain(CANARY)

  const dataDir = process.env.FME_E2E_DATA_DIR ?? ''
  expect(dataDir).not.toBe('')
  const files = filesUnder(dataDir)
  expect(files.some((file) => file.endsWith('.sqlite3'))).toBe(true)
  expect(files.some((file) => file.includes('logs'))).toBe(true)
  for (const file of files) expect(readFileSync(file).includes(CANARY), file).toBe(false)
})
