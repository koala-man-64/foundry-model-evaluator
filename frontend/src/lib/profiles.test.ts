import { describe, expect, it } from 'vitest'

import type { ModelProfile } from '../api/types'
import { modelArgument, reportedModel, requestModel, sameModel, variantFields, type VariantInput } from './profiles'

function profile(overrides: Partial<ModelProfile> = {}): ModelProfile {
  return {
    id: 'p-1',
    endpoint_id: 'ep-1',
    name: 'Claude Opus (CLI)',
    api_style: 'claude_cli',
    route: 'cli',
    custom_path: null,
    api_version: null,
    deployment: 'opus',
    publisher: 'Anthropic',
    model_name: 'Claude Opus',
    model_version: '',
    context_window: 200000,
    max_output_tokens: 16384,
    preset: 'claude-cli',
    capabilities: { settings: {} },
    deployment_type: 'global',
    processing_tier: 'standard',
    pricing_region: null,
    meter_terms: 'opus terms',
    long_context_threshold: null,
    streaming: false,
    include_usage: true,
    timeout_connect_s: 15,
    timeout_read_s: 600,
    timeout_total_s: 900,
    notes: 'source notes',
    archived: false,
    created_at: '2026-01-01T00:00:00Z',
    updated_at: '2026-01-01T00:00:00Z',
    ...overrides,
  } as ModelProfile
}

const input = (overrides: Partial<VariantInput> = {}): VariantInput => ({
  deployment: 'claude-opus-4-1',
  modelVersion: '20250805',
  modelName: 'Claude Opus',
  meterTerms: '',
  name: '',
  ...overrides,
})

describe('requestModel', () => {
  it('pins a CLI snapshot by appending the version, as the backend does', () => {
    expect(requestModel(profile({ deployment: 'claude-opus-4-1', model_version: ' 20250805 ' }))).toBe('claude-opus-4-1-20250805')
    expect(requestModel(profile({ api_style: 'codex_cli', deployment: 'gpt-4.1', model_version: '2025-04-14' }))).toBe(
      'gpt-4.1-2025-04-14',
    )
    expect(requestModel(profile({ deployment: 'opus' }))).toBe('opus')
  })

  it('sends a Foundry deployment as it is: the deployment fixes the version', () => {
    expect(requestModel(profile({ api_style: 'openai_chat', deployment: 'my-gpt41', model_version: '2025-04-14' }))).toBe(
      'my-gpt41',
    )
  })

  it('names the flag each CLI takes', () => {
    expect(modelArgument(profile({ model_version: '20250805', deployment: 'claude-opus-4-1' }))).toBe(
      '--model claude-opus-4-1-20250805',
    )
    expect(modelArgument(profile({ api_style: 'codex_cli', deployment: 'o3' }))).toBe('-m o3')
    expect(modelArgument(profile({ api_style: 'openai_responses', deployment: 'my-gpt' }))).toBe('model my-gpt')
  })
})

describe('reportedModel', () => {
  it('shows what answered only when it says more than what was sent', () => {
    expect(reportedModel(profile(), 'claude-opus-4-5-20251101')).toBe('claude-opus-4-5-20251101')
    expect(reportedModel(profile(), 'opus')).toBeNull()
    expect(reportedModel(profile({ deployment: 'claude-opus-4-1', model_version: '20250805' }), 'claude-opus-4-1-20250805')).toBeNull()
    expect(reportedModel(profile(), null)).toBeNull()
  })
})

describe('variantFields', () => {
  it('copies the settings and replaces the model identity, never the rates or meter terms', () => {
    const source = profile()
    const fields = variantFields(source, input({ modelName: ' Claude Opus 4.1 ' }))
    expect(fields).not.toHaveProperty('id')
    expect(fields).not.toHaveProperty('archived')
    expect(fields).not.toHaveProperty('created_at')
    expect(fields).toMatchObject({
      endpoint_id: 'ep-1',
      api_style: 'claude_cli',
      route: 'cli',
      preset: 'claude-cli',
      max_output_tokens: 16384,
      publisher: 'Anthropic',
      deployment: 'claude-opus-4-1',
      model_version: '20250805',
      model_name: 'Claude Opus 4.1',
      meter_terms: '',
      notes: 'Variant of Claude Opus (CLI).',
      name: 'claude-opus-4-1-20250805',
    })
  })

  it('keeps a typed name and suggests one with the recorded Foundry version', () => {
    expect(variantFields(profile(), input({ name: ' Opus 4.1 ' })).name).toBe('Opus 4.1')
    const foundry = profile({ api_style: 'openai_chat', route: 'openai_v1', deployment: 'my-gpt41' })
    expect(variantFields(foundry, input({ deployment: 'my-gpt41-mini', modelVersion: '2025-04-14' })).name).toBe(
      'my-gpt41-mini 2025-04-14',
    )
  })

  it('treats the same model and version as no variant', () => {
    expect(sameModel(profile(), input({ deployment: ' opus ', modelVersion: '' }))).toBe(true)
    expect(sameModel(profile(), input({ deployment: 'opus', modelVersion: '20250805' }))).toBe(false)
  })
})
