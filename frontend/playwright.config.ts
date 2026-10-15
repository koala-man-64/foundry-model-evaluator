import { defineConfig } from '@playwright/test'
import { mkdtempSync } from 'node:fs'
import { tmpdir } from 'node:os'
import { dirname, join, resolve } from 'node:path'
import { fileURLToPath } from 'node:url'

// The app runs against the mock Foundry server on a throwaway data directory (outside the repo),
// with an in-memory credential store. Nothing here can make paid calls.
const APP_PORT = 8777
const MOCK_PORT = 8778
// Created once by the runner; workers inherit the variable, so tests can scan the same folder.
const dataDir = process.env.FME_E2E_DATA_DIR ?? mkdtempSync(join(tmpdir(), 'fme-e2e-'))
process.env.FME_E2E_DATA_DIR = dataDir
const here = dirname(fileURLToPath(import.meta.url))
const backend = resolve(here, '../backend')

export default defineConfig({
  testDir: './e2e',
  fullyParallel: false,
  workers: 1,
  timeout: 90_000,
  expect: { timeout: 15_000 },
  reporter: process.env.CI ? [['list'], ['html', { open: 'never' }]] : 'list',
  use: {
    baseURL: `http://127.0.0.1:${APP_PORT}`,
    trace: 'retain-on-failure',
    screenshot: 'only-on-failure',
  },
  webServer: [
    {
      command: `uv run --project "${backend}" python -m foundry_eval.devtools.mock_foundry --port ${MOCK_PORT}`,
      url: `http://127.0.0.1:${MOCK_PORT}/__mock/stats`,
      reuseExistingServer: false,
      timeout: 120_000,
    },
    {
      command: `uv run --project "${backend}" foundry-eval serve --no-browser --port ${APP_PORT}`,
      url: `http://127.0.0.1:${APP_PORT}/api/health`,
      reuseExistingServer: false,
      timeout: 120_000,
      env: {
        FME_DATA_DIR: dataDir,
        FME_KEYRING: 'memory',
        FME_RETAIL_PRICES_URL: `http://127.0.0.1:${MOCK_PORT}/api/retail/prices`,
        FME_STATIC_DIR: resolve(here, 'dist'),
      },
    },
  ],
  projects: [
    {
      name: 'chromium',
      use: {
        browserName: 'chromium',
        launchOptions: process.env.PLAYWRIGHT_CHROMIUM_EXECUTABLE
          ? { executablePath: process.env.PLAYWRIGHT_CHROMIUM_EXECUTABLE }
          : {},
      },
    },
  ],
})
