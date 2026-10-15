import { expect, type APIRequestContext, type Locator, type Page } from '@playwright/test'

export const APP_URL = 'http://127.0.0.1:8777'
export const MOCK_URL = 'http://127.0.0.1:8778'
export const CANARY = 'sk-e2e-canary-91c4f0d2a7'

export function unique(prefix: string): string {
  return `${prefix}-${Date.now().toString(36)}${Math.floor(Math.random() * 1000)}`
}

/** Collect page errors and CSP violations; tests assert the list stays empty. */
export function watchConsole(page: Page): string[] {
  const problems: string[] = []
  page.on('pageerror', (error) => problems.push(`pageerror: ${error.message}`))
  page.on('console', (message) => {
    const text = message.text()
    if (message.type() === 'error' && !text.includes('Failed to load resource')) problems.push(`console: ${text}`)
    if (text.toLowerCase().includes('content security policy')) problems.push(`csp: ${text}`)
  })
  return problems
}

/** Text of localStorage and sessionStorage, for secret scans. */
export async function browserStorage(page: Page): Promise<string> {
  return page.evaluate(() => {
    const dump = (storage: Storage) =>
      Array.from({ length: storage.length }, (_, i) => {
        const key = storage.key(i) ?? ''
        return `${key}=${storage.getItem(key) ?? ''}`
      }).join('\n')
    return `${dump(window.localStorage)}\n${dump(window.sessionStorage)}`
  })
}

// ---------------------------------------------------------------- UI helpers

export async function createEndpoint(page: Page, name: string): Promise<void> {
  await page.goto('/models/endpoints')
  await page.getByRole('button', { name: 'Add endpoint' }).click()
  const form = page.getByRole('form', { name: 'New endpoint' })
  await form.getByLabel('Name').fill(name)
  await form.getByLabel('Resource URL').fill(MOCK_URL)
  await form.getByLabel('Azure region').fill('eastus2')
  await form.getByRole('button', { name: 'Save endpoint' }).click()
  const card = page.getByTestId('endpoint').filter({ hasText: name })
  await expect(card).toBeVisible()
  const keyInput = card.getByLabel(`API key for ${name}`)
  await keyInput.fill(CANARY)
  await card.getByRole('button', { name: 'Save key' }).click()
  await expect(card.getByText('key stored')).toBeVisible()
  await expect(keyInput).toHaveValue('')
}

interface ProfileOptions {
  endpoint: string
  name: string
  api: 'OpenAI Responses' | 'Chat Completions' | 'Claude Messages'
  preset?: string
  deployment: string
  meterTerms?: string
  contextWindow?: string
  longContextThreshold?: string
}

export async function createProfile(page: Page, options: ProfileOptions): Promise<void> {
  await page.goto('/models/profiles')
  await page.getByRole('button', { name: 'Add model profile' }).click()
  const form = page.getByRole('form', { name: 'New model profile' })
  await form.getByLabel('Display name').fill(options.name)
  await form.getByLabel('Endpoint').selectOption({ label: options.endpoint })
  await form.getByLabel('API', { exact: true }).selectOption({ label: options.api })
  if (options.preset) await form.getByLabel('Preset').selectOption({ label: options.preset })
  await form.getByLabel('Deployment name').fill(options.deployment)
  if (options.meterTerms) await form.getByLabel('Meter terms').fill(options.meterTerms)
  if (options.contextWindow) await form.getByLabel('Context window (tokens)').fill(options.contextWindow)
  if (options.longContextThreshold)
    await form.getByLabel('Long-context threshold (input tokens)').fill(options.longContextThreshold)
  await form.getByRole('button', { name: 'Save profile' }).click()
  await expect(form).toBeHidden()
  await expect(page.getByRole('row').filter({ hasText: options.name })).toBeVisible()
}

/** The rate table row for a category in the base tier (or the given tier). */
export function rateRow(page: Page, category: string, tier = 0) {
  return page.getByTestId(`rate-${category}-${tier}`)
}

export async function setManualRate(page: Page, category: string, price: string, tier = 0): Promise<void> {
  await rateRow(page, category, tier).getByRole('button', { name: 'Manual' }).click()
  const dialog = page.getByRole('dialog')
  await dialog.getByLabel('Price (USD)').fill(price)
  await dialog.getByLabel('Source URL (required)').fill('https://www.anthropic.com/pricing')
  await dialog.getByRole('button', { name: 'Save' }).click()
  await expect(dialog).toBeHidden()
}

// ---------------------------------------------------------------- API helpers (fast setup)

const WRITE_HEADERS = { Origin: APP_URL, 'X-FME-Client': '1' }

export class AppApi {
  constructor(private readonly request: APIRequestContext) {}

  async call<T>(method: string, path: string, data?: unknown, headers: Record<string, string> = {}): Promise<T> {
    const response = await this.request.fetch(`${APP_URL}${path}`, {
      method,
      data: data === undefined ? undefined : (data as object),
      headers: method === 'GET' ? headers : { ...WRITE_HEADERS, ...headers },
    })
    if (!response.ok()) throw new Error(`${method} ${path} failed: ${response.status()} ${await response.text()}`)
    return (response.status() === 204 ? undefined : await response.json()) as T
  }

  async text(path: string): Promise<string> {
    const response = await this.request.get(`${APP_URL}${path}`)
    expect(response.ok()).toBeTruthy()
    return response.text()
  }

  async endpoint(name: string, key: string = CANARY): Promise<string> {
    const endpoint = await this.call<{ id: string }>('POST', '/api/endpoints', {
      name,
      base_url: MOCK_URL,
      region: 'eastus2',
      auth_style: 'api-key',
    })
    await this.call('PUT', `/api/endpoints/${endpoint.id}/key`, { api_key: key })
    return endpoint.id
  }

  async profile(fields: {
    endpointId: string
    name: string
    apiStyle: 'openai_responses' | 'openai_chat' | 'anthropic_messages'
    preset: string
    deployment: string
    meterTerms?: string
    longContextThreshold?: number
    extra?: Record<string, unknown>
  }): Promise<string> {
    const presets = await this.call<{ presets: { id: string; capabilities: Record<string, unknown> }[] }>(
      'GET',
      '/api/capability-presets',
    )
    const preset = presets.presets.find((p) => p.id === fields.preset)
    if (!preset) throw new Error(`unknown preset ${fields.preset}`)
    const route = fields.apiStyle === 'anthropic_messages' ? 'anthropic' : 'openai_v1'
    const profile = await this.call<{ id: string }>('POST', '/api/profiles', {
      endpoint_id: fields.endpointId,
      name: fields.name,
      api_style: fields.apiStyle,
      route,
      deployment: fields.deployment,
      preset: fields.preset,
      capabilities: preset.capabilities[fields.apiStyle],
      meter_terms: fields.meterTerms ?? '',
      long_context_threshold: fields.longContextThreshold ?? null,
      ...fields.extra,
    })
    return profile.id
  }

  /** Start a run through the API and wait until it (and any judging) has finished. */
  async run(request: Record<string, unknown>): Promise<string> {
    const created = await this.call<{ run_id: string }>('POST', '/api/runs', { kind: 'compare', ...request }, {
      'Idempotency-Key': crypto.randomUUID(),
    })
    await this.waitForRun(created.run_id)
    return created.run_id
  }

  async manualRate(profileId: string, category: string, price: string): Promise<void> {
    await this.call('PUT', `/api/profiles/${profileId}/rates/${category}`, {
      tier: 0,
      mode: 'manual',
      price,
      unit: '1M',
      source_url: 'https://www.anthropic.com/pricing',
    })
  }

  /** Manual rates for every Claude category (input, cache read, 5m/1h writes, output). */
  async claudeRates(profileId: string, rates = { input: '3', read: '0.30', w5m: '3.75', w1h: '6', output: '15' }) {
    await this.manualRate(profileId, 'input_uncached', rates.input)
    await this.manualRate(profileId, 'cache_read', rates.read)
    await this.manualRate(profileId, 'cache_write_5m', rates.w5m)
    await this.manualRate(profileId, 'cache_write_1h', rates.w1h)
    await this.manualRate(profileId, 'output', rates.output)
  }

  async refreshPrices(): Promise<void> {
    await this.call('POST', '/api/pricing/refresh', {})
  }

  async setConcurrency(value: number): Promise<void> {
    const current = await this.call<{ settings: Record<string, unknown> }>('GET', '/api/settings')
    await this.call('PUT', '/api/settings', { ...current.settings, concurrency: value })
  }

  async waitForRun(runId: string): Promise<{ run: { status: string }; active: boolean }> {
    let detail: { run: { status: string }; active: boolean } | undefined
    await expect
      .poll(
        async () => {
          detail = await this.call('GET', `/api/runs/${runId}`)
          return detail?.active === false && !['queued', 'running'].includes(detail.run.status)
        },
        { timeout: 60_000, intervals: [250, 500, 1000] },
      )
      .toBe(true)
    return detail!
  }
}

export interface MockStats {
  requests: Record<string, number>
  key_hashes: string[]
  cancelled: number
}

export async function mockStats(request: APIRequestContext): Promise<MockStats> {
  const response = await request.get(`${MOCK_URL}/__mock/stats`)
  return (await response.json()) as MockStats
}

/** Fill the Compare page for one candidate card. */
export function candidateCard(page: Page, index: number) {
  return page.getByTestId('candidate').nth(index)
}

function escapeRegExp(text: string): string {
  return text.replace(/[.*+?^${}()|[\]\\]/g, '\\$&')
}

/** Pick a model profile for a Compare candidate; options read "<profile name> — <API>". */
export async function chooseProfile(page: Page, index: number, profileName: string): Promise<void> {
  const select = page.getByLabel(`Model profile for candidate ${index + 1}`)
  const option = select.locator('option', { hasText: new RegExp(`^${escapeRegExp(profileName)} — `) })
  await expect(option).toHaveCount(1)
  const value = await option.getAttribute('value')
  if (!value) throw new Error(`profile ${profileName} not offered`)
  await select.selectOption(value)
}

/** Select an option of a native select by (part of) its visible text. */
export async function selectByText(select: Locator, text: string | RegExp): Promise<void> {
  const option = select.locator('option', { hasText: text }).first()
  const value = await option.getAttribute('value')
  if (value === null) throw new Error(`option ${String(text)} not found`)
  await select.selectOption(value)
}
