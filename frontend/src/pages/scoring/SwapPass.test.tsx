import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { render, screen, waitFor } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import type { ReactNode } from 'react'
import { beforeEach, describe, expect, it, vi } from 'vitest'

import type { EvaluationOut, JudgePreview } from '../../api/types'
import { SwapPass, swappable } from './SwapPass'

const calls: { path: string; body: unknown; key?: string }[] = []
let previewResult: Partial<JudgePreview> = {}
let paidFails = false
let keys = 0

vi.mock('../../api/client', () => ({
  newIdempotencyKey: () => `key-${++keys}`,
  post: (path: string, body: unknown, key?: string) => {
    calls.push({ path, body, key })
    if (path.endsWith('/swap/preview')) return Promise.resolve(preview(previewResult))
    if (paidFails) return Promise.reject(new Error('connection lost'))
    return new Promise(() => {}) // the paid request stays pending, as a slow server would
  },
}))

function preview(partial: Partial<JudgePreview>): JudgePreview {
  return {
    ok: true,
    errors: [],
    warnings: [],
    judge_profile_name: 'Judge',
    rubric_version_id: 'default-v1',
    rubric_name: 'Default rubric',
    rubric_version: 1,
    structured_output: true,
    key_configured: true,
    participant_reasons: [],
    groups: [],
    skipped_groups: 0,
    calls: 1,
    worst_case_total: '0.0123',
    unknown_rates: [],
    ...partial,
  }
}

const source = {
  id: 'ev-1',
  status: 'succeeded',
  swap_of: null,
  candidates: [{}, {}],
} as unknown as EvaluationOut

function wrapper({ children }: { children: ReactNode }) {
  return <QueryClientProvider client={new QueryClient()}>{children}</QueryClientProvider>
}

beforeEach(() => {
  calls.length = 0
  previewResult = {}
  paidFails = false
})

describe('SwapPass', () => {
  it('previews the reversed re-judging before anything is paid for', async () => {
    const user = userEvent.setup()
    render(<SwapPass runId="run-1" evaluation={source} />, { wrapper })
    expect(calls).toEqual([]) // nothing is requested until the control is opened
    await user.click(screen.getByRole('button', { name: 'Swap order and re-judge…' }))
    expect(await screen.findByTestId('swap-preview')).toHaveTextContent('1 judge call · worst-case estimated token cost')
    expect(calls).toEqual([{ path: '/api/runs/run-1/evaluations/swap/preview', body: { evaluation_id: 'ev-1', scope: 'evaluation' }, key: undefined }])

    await user.selectOptions(screen.getByRole('combobox'), 'run')
    await waitFor(() => expect(calls.at(-1)?.body).toEqual({ evaluation_id: 'ev-1', scope: 'run' }))
    expect(calls.every((c) => c.path.endsWith('/swap/preview'))).toBe(true)
  })

  it('sends the paid request once, with an idempotency key, only when confirmed', async () => {
    const user = userEvent.setup()
    render(<SwapPass runId="run-1" evaluation={source} />, { wrapper })
    await user.click(screen.getByRole('button', { name: 'Swap order and re-judge…' }))
    const run = await screen.findByRole('button', { name: 'Run swap pass' })
    await waitFor(() => expect(run).toBeEnabled())
    await user.click(run)
    const paid = calls.filter((c) => c.path === '/api/runs/run-1/evaluations/swap')
    expect(paid).toHaveLength(1)
    expect(paid[0].key).toMatch(/^key-\d+$/)
    // While it is pending the button is disabled, so a second click cannot send it again.
    expect(screen.getByRole('button', { name: 'Starting…' })).toBeDisabled()
  })

  it('never confirms the paid request with a preview of another scope', async () => {
    const user = userEvent.setup()
    render(<SwapPass runId="run-1" evaluation={source} />, { wrapper })
    await user.click(screen.getByRole('button', { name: 'Swap order and re-judge…' }))
    const run = await screen.findByRole('button', { name: 'Run swap pass' })
    await waitFor(() => expect(run).toBeEnabled())
    await user.selectOptions(screen.getByRole('combobox'), 'run')
    // The one-evaluation preview is still on screen, but it cannot confirm the run-wide request.
    expect(run).toBeDisabled()
    await waitFor(() => expect(run).toBeEnabled())
    await user.click(run)
    const paid = calls.filter((c) => c.path === '/api/runs/run-1/evaluations/swap')
    expect(paid.map((c) => c.body)).toEqual([{ evaluation_id: 'ev-1', scope: 'run' }])
  })

  it('retries with the same key, but uses a new key once the scope changes', async () => {
    paidFails = true
    const user = userEvent.setup()
    render(<SwapPass runId="run-1" evaluation={source} />, { wrapper })
    await user.click(screen.getByRole('button', { name: 'Swap order and re-judge…' }))
    const run = await screen.findByRole('button', { name: 'Run swap pass' })
    await waitFor(() => expect(run).toBeEnabled())
    await user.click(run)
    await screen.findByText('connection lost')
    await user.click(run)
    await user.selectOptions(screen.getByRole('combobox'), 'run')
    await waitFor(() => expect(run).toBeEnabled())
    await user.click(run)
    const paid = calls.filter((c) => c.path === '/api/runs/run-1/evaluations/swap').map((c) => c.key)
    expect(paid).toHaveLength(3)
    expect(paid[1]).toBe(paid[0]) // a lost response retried as the same request is not paid twice
    expect(paid[2]).not.toBe(paid[0])
  })

  it('cannot be run when the preview reports errors', async () => {
    previewResult = { ok: false, calls: 0, errors: ['This evaluation cannot be swapped: it already has a swap pass.'] }
    const user = userEvent.setup()
    render(<SwapPass runId="run-1" evaluation={source} />, { wrapper })
    await user.click(screen.getByRole('button', { name: 'Swap order and re-judge…' }))
    expect(await screen.findByRole('alert')).toHaveTextContent('already has a swap pass')
    expect(screen.getByRole('button', { name: 'Run swap pass' })).toBeDisabled()
  })

  it('is offered only for succeeded comparisons that are not swaps themselves', () => {
    expect(swappable(source)).toBe(true)
    expect(swappable({ ...source, swap_of: 'ev-0' })).toBe(false)
    expect(swappable({ ...source, status: 'malformed' })).toBe(false)
    expect(swappable({ ...source, candidates: [source.candidates[0]] })).toBe(false)
  })
})
