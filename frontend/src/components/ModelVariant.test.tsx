import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { render, screen, waitFor } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import type { ReactNode } from 'react'
import { beforeEach, describe, expect, it, vi } from 'vitest'

import type { ModelProfile } from '../api/types'
import { CandidateModel } from './ModelVariant'

const calls: { path: string; body: Record<string, unknown> }[] = []

vi.mock('../api/client', () => ({
  post: (path: string, body: Record<string, unknown>) => {
    calls.push({ path, body })
    return Promise.resolve({ ...body, id: 'p-variant', archived: false, created_at: 'now', updated_at: 'now' })
  },
}))

const source = {
  id: 'p-1',
  endpoint_id: 'ep-1',
  name: 'Claude Opus (CLI)',
  api_style: 'claude_cli',
  route: 'cli',
  deployment: 'opus',
  publisher: 'Anthropic',
  model_name: 'Claude Opus',
  model_version: '',
  meter_terms: '',
  preset: 'claude-cli',
  capabilities: { settings: {} },
  notes: '',
  archived: false,
  created_at: '2026-01-01T00:00:00Z',
  updated_at: '2026-01-01T00:00:00Z',
} as unknown as ModelProfile

function wrapper({ children }: { children: ReactNode }) {
  return <QueryClientProvider client={new QueryClient()}>{children}</QueryClientProvider>
}

beforeEach(() => {
  calls.length = 0
})

describe('CandidateModel', () => {
  it('saves a variant pinned to a snapshot and hands it to the candidate', async () => {
    const user = userEvent.setup()
    const onVariant = vi.fn()
    render(<CandidateModel profile={source} candidateLabel="candidate 1" onVariant={onVariant} />, { wrapper })
    expect(screen.getByText('--model opus')).toBeInTheDocument()

    await user.click(screen.getByRole('button', { name: 'New variant…' }))
    const model = screen.getByRole('textbox', { name: 'Model' })
    await user.clear(model)
    await user.type(model, 'claude-opus-4-1')
    await user.type(screen.getByRole('textbox', { name: 'Model version' }), '20250805')
    expect(screen.getByText('--model claude-opus-4-1-20250805')).toBeInTheDocument()
    // A CLI has no Azure meters to match.
    expect(screen.queryByRole('textbox', { name: 'Meter terms' })).not.toBeInTheDocument()

    await user.click(screen.getByRole('button', { name: 'Save variant' }))
    await waitFor(() => expect(onVariant).toHaveBeenCalledWith(expect.objectContaining({ id: 'p-variant' })))
    expect(calls).toHaveLength(1)
    expect(calls[0].path).toBe('/api/profiles')
    expect(calls[0].body).toMatchObject({ deployment: 'claude-opus-4-1', model_version: '20250805', endpoint_id: 'ep-1' })
    expect(screen.queryByRole('group', { name: 'New variant for candidate 1' })).not.toBeInTheDocument()
  })

  it('refuses a variant with the same model and version', async () => {
    const user = userEvent.setup()
    render(<CandidateModel profile={source} candidateLabel="candidate 1" onVariant={vi.fn()} />, { wrapper })
    await user.click(screen.getByRole('button', { name: 'New variant…' }))
    await user.click(screen.getByRole('button', { name: 'Save variant' }))
    expect(screen.getByText('Change the model or the version: this is Claude Opus (CLI).')).toBeInTheDocument()
    expect(calls).toHaveLength(0)
  })

  it('saves on Enter without submitting a surrounding form', async () => {
    const user = userEvent.setup()
    const onSubmit = vi.fn((event: { preventDefault: () => void }) => event.preventDefault())
    const onVariant = vi.fn()
    render(
      <form onSubmit={onSubmit}>
        <CandidateModel profile={source} candidateLabel="suite candidate 1" onVariant={onVariant} />
      </form>,
      { wrapper },
    )
    await user.click(screen.getByRole('button', { name: 'New variant…' }))
    await user.type(screen.getByRole('textbox', { name: 'Model version' }), '20250805{Enter}')
    await waitFor(() => expect(onVariant).toHaveBeenCalled())
    expect(onSubmit).not.toHaveBeenCalled()
    expect(calls[0].body).toMatchObject({ deployment: 'opus', model_version: '20250805' })
  })
})
