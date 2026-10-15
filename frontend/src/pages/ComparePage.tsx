import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import { useEffect, useMemo, useRef, useState } from 'react'
import { Link, useNavigate } from 'react-router'

import { ApiError, get, newIdempotencyKey, post } from '../api/client'
import { useProfiles } from '../api/hooks'
import type { GenerationSettings, ModelProfile, RunPreview, RunRequest } from '../api/types'
import { cleanSettings, SettingsEditor, unsupportedSettings } from '../components/SettingsEditor'
import { Badge, Empty, ErrorMessage, Field, Spinner } from '../components/ui'
import { API_STYLE_LABELS } from '../lib/format'
import { formatMoney, parseDecimal } from '../lib/money'

interface CandidateDraft {
  key: string
  profileId: string
  label: string
  settings: GenerationSettings
}

interface Draft {
  system: string
  user: string
  title: string
  budget: string
  candidates: CandidateDraft[]
  suitePromptId: string | null
}

// Kept in memory only (never in browser storage) so a draft survives navigation within the session.
let draftMemory: Draft = { system: '', user: '', title: '', budget: '', candidates: [], suitePromptId: null }

function defaultSettings(profile: ModelProfile | undefined): GenerationSettings {
  const spec = profile?.capabilities.settings?.max_output_tokens
  if (spec?.supported && spec.required) return { max_output_tokens: Math.min(4096, spec.maximum ?? 4096) }
  return {}
}

let candidateCounter = 0
function newCandidate(profile: ModelProfile | undefined): CandidateDraft {
  candidateCounter += 1
  return { key: `c${Date.now()}-${candidateCounter}`, profileId: profile?.id ?? '', label: '', settings: defaultSettings(profile) }
}

interface SuitePromptOption {
  id: string
  suiteName: string
  title: string
  system: string
  user: string
}

function useSuitePrompts() {
  return useQuery({
    queryKey: ['suite-prompts'],
    queryFn: async () => {
      try {
        const suites = await get<{ id: string; name: string; prompts: { id: string; title: string; system: string; user: string }[] }[]>(
          '/api/suites',
        )
        return suites.flatMap((suite) =>
          suite.prompts.map((p) => ({ id: p.id, suiteName: suite.name, title: p.title, system: p.system, user: p.user })),
        ) as SuitePromptOption[]
      } catch (error) {
        if (error instanceof ApiError && error.status === 404) return [] as SuitePromptOption[]
        throw error
      }
    },
  })
}

export function ComparePage() {
  const navigate = useNavigate()
  const profilesQuery = useProfiles()
  const suitePrompts = useSuitePrompts()
  const profiles = useMemo(() => profilesQuery.data ?? [], [profilesQuery.data])
  const [draft, setDraftState] = useState<Draft>(draftMemory)
  const setDraft = (update: (d: Draft) => Draft) =>
    setDraftState((current) => {
      const next = update(current)
      draftMemory = next
      return next
    })
  const [preview, setPreview] = useState<RunPreview | null>(null)
  const [previewError, setPreviewError] = useState<unknown>(null)
  const runKey = useRef<string>(newIdempotencyKey())
  const queryClient = useQueryClient()

  useEffect(() => {
    if (profiles.length && draft.candidates.length === 0) {
      setDraft((d) => ({ ...d, candidates: [newCandidate(profiles[0])] }))
    }
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [profiles])

  const profileById = useMemo(() => new Map(profiles.map((p) => [p.id, p])), [profiles])
  const budgetValid = draft.budget.trim() === '' || parseDecimal(draft.budget.trim()) !== null
  const request: RunRequest | null = useMemo(() => {
    if (!draft.user.trim() || draft.candidates.length === 0 || draft.candidates.some((c) => !c.profileId)) return null
    if (!budgetValid) return null
    return {
      kind: 'compare',
      title: draft.title,
      prompts: [{ system: draft.system, user: draft.user, title: '', suite_prompt_id: draft.suitePromptId }],
      candidates: draft.candidates.map((c) => ({
        profile_id: c.profileId,
        label: c.label.trim() || null,
        settings: cleanSettings(c.settings),
      })),
      trials: 1,
      budget: draft.budget.trim() ? draft.budget.trim() : null,
    }
  }, [draft, budgetValid])

  useEffect(() => {
    if (!request) {
      setPreview(null)
      return
    }
    const timer = window.setTimeout(() => {
      post<RunPreview>('/api/runs/preview', request)
        .then((result) => {
          setPreview(result)
          setPreviewError(null)
        })
        .catch((error) => setPreviewError(error))
    }, 350)
    return () => window.clearTimeout(timer)
  }, [request])

  const start = useMutation({
    mutationFn: (body: RunRequest) => post<{ run_id: string }>('/api/runs', body, runKey.current),
    onSuccess: (result) => {
      runKey.current = newIdempotencyKey()
      queryClient.invalidateQueries({ queryKey: ['runs'] })
      navigate(`/runs/${result.run_id}`)
    },
  })

  const blocking = draft.candidates.flatMap((c) =>
    unsupportedSettings(profileById.get(c.profileId)?.capabilities, c.settings).map(
      (label) => `${c.label || profileById.get(c.profileId)?.name}: ${label} is set but not supported`,
    ),
  )
  const canRun = Boolean(request && preview?.ok && blocking.length === 0 && !start.isPending)

  if (profilesQuery.isLoading) return <Spinner />
  if (profiles.length === 0) {
    return (
      <Empty>
        No model profiles yet. <Link to="/models/endpoints">Add an endpoint and a model profile</Link> first.
      </Empty>
    )
  }

  const updateCandidate = (key: string, update: Partial<CandidateDraft>) =>
    setDraft((d) => ({ ...d, candidates: d.candidates.map((c) => (c.key === key ? { ...c, ...update } : c)) }))

  return (
    <div className="stack">
      <section className="card stack" aria-label="Prompt">
        <div className="row-between">
          <h2>Prompt</h2>
          {(suitePrompts.data ?? []).length > 0 && (
            <label className="row small">
              Load from suite
              <select
                value=""
                onChange={(e) => {
                  const option = suitePrompts.data?.find((p) => p.id === e.target.value)
                  if (option)
                    setDraft((d) => ({ ...d, system: option.system, user: option.user, suitePromptId: option.id, title: option.title }))
                }}
              >
                <option value="">Choose a saved prompt…</option>
                {suitePrompts.data?.map((p) => (
                  <option key={p.id} value={p.id}>
                    {p.suiteName}: {p.title || p.user.slice(0, 50)}
                  </option>
                ))}
              </select>
            </label>
          )}
        </div>
        <Field label="System instructions (optional)">
          <textarea
            value={draft.system}
            onChange={(e) => setDraft((d) => ({ ...d, system: e.target.value, suitePromptId: null }))}
            placeholder="Optional instructions sent as the system / developer message"
          />
        </Field>
        <Field label="User prompt">
          <textarea
            className="prompt"
            value={draft.user}
            onChange={(e) => setDraft((d) => ({ ...d, user: e.target.value, suitePromptId: null }))}
            placeholder="What should every model answer?"
          />
        </Field>
      </section>

      <section className="stack" aria-label="Candidates">
        <div className="row-between">
          <h2>Models and settings</h2>
          <button type="button" onClick={() => setDraft((d) => ({ ...d, candidates: [...d.candidates, newCandidate(profiles[0])] }))}>
            Add candidate
          </button>
        </div>
        {draft.candidates.map((candidate, index) => {
          const profile = profileById.get(candidate.profileId)
          const issues = preview?.candidates.find((c) => c.index === index)?.issues ?? []
          const info = preview?.candidates.find((c) => c.index === index)
          return (
            <div key={candidate.key} className="card stack" data-testid="candidate">
              <div className="row-between">
                <div className="row grow">
                  <Field label={`Candidate ${index + 1}`}>
                    <select
                      aria-label={`Model profile for candidate ${index + 1}`}
                      value={candidate.profileId}
                      onChange={(e) =>
                        // Existing values are kept (and flagged if unsupported); only missing required ones get defaults.
                        updateCandidate(candidate.key, {
                          profileId: e.target.value,
                          settings: { ...defaultSettings(profileById.get(e.target.value)), ...candidate.settings },
                        })
                      }
                    >
                      {profiles.map((p) => (
                        <option key={p.id} value={p.id}>
                          {p.name} — {API_STYLE_LABELS[p.api_style] ?? p.api_style}
                        </option>
                      ))}
                    </select>
                  </Field>
                  <Field label="Label (optional)">
                    <input
                      value={candidate.label}
                      placeholder={info?.label ?? 'auto'}
                      onChange={(e) => updateCandidate(candidate.key, { label: e.target.value })}
                    />
                  </Field>
                </div>
                <div className="row">
                  <button
                    type="button"
                    className="small"
                    onClick={() =>
                      setDraft((d) => {
                        const copy = { ...candidate, key: newCandidate(profile).key, label: '', settings: { ...candidate.settings } }
                        const list = [...d.candidates]
                        list.splice(index + 1, 0, copy)
                        return { ...d, candidates: list }
                      })
                    }
                  >
                    Duplicate
                  </button>
                  {draft.candidates.length > 1 && (
                    <button
                      type="button"
                      className="small danger"
                      onClick={() => setDraft((d) => ({ ...d, candidates: d.candidates.filter((c) => c.key !== candidate.key) }))}
                    >
                      Remove
                    </button>
                  )}
                </div>
              </div>
              {info && (
                <div className="row small">
                  {!info.key_configured && <Badge tone="error">no API key</Badge>}
                  {info.pricing_complete ? (
                    <Badge tone="ok">rates complete</Badge>
                  ) : (
                    <Badge tone="warn" title={info.unknown_rates.join(', ')}>
                      rates unknown: {info.unknown_rates.length}
                    </Badge>
                  )}
                  {info.custom_rates.length > 0 && <Badge tone="custom">custom rates</Badge>}
                  {info.worst_case_per_call && (
                    <span className="muted">worst case per call {formatMoney(info.worst_case_per_call)}</span>
                  )}
                </div>
              )}
              <details open={index === 0 || issues.length > 0}>
                <summary>Generation settings</summary>
                <SettingsEditor
                  idPrefix={candidate.key}
                  capabilities={profile?.capabilities}
                  value={candidate.settings}
                  issues={issues}
                  onChange={(settings) => updateCandidate(candidate.key, { settings })}
                />
              </details>
            </div>
          )
        })}
      </section>

      <section className="card stack" aria-label="Run">
        <div className="grid-form">
          <Field label="Run title (optional)">
            <input value={draft.title} onChange={(e) => setDraft((d) => ({ ...d, title: e.target.value }))} />
          </Field>
          <Field
            label="Budget (optional, USD)"
            hint="Calls whose worst-case cost could exceed the budget are skipped."
            error={budgetValid ? null : 'Enter a decimal amount such as 0.50'}
          >
            <input
              inputMode="decimal"
              value={draft.budget}
              placeholder="e.g. 0.50"
              onChange={(e) => setDraft((d) => ({ ...d, budget: e.target.value }))}
            />
          </Field>
        </div>
        {preview && (
          <div className="stack-sm">
            <p className="small">
              {preview.calls} call{preview.calls === 1 ? '' : 's'} · worst-case estimated token cost{' '}
              {preview.worst_case_total ? formatMoney(preview.worst_case_total) : 'not bounded (set max output tokens and rates)'}
            </p>
            {preview.errors.length > 0 && (
              <div className="callout error small" role="alert">
                <strong>Fix before running</strong>
                <ul>
                  {preview.errors.map((e) => (
                    <li key={e}>{e}</li>
                  ))}
                </ul>
              </div>
            )}
            {preview.warnings.length > 0 && (
              <div className="callout warn small">
                <ul>
                  {preview.warnings.map((w) => (
                    <li key={w}>{w}</li>
                  ))}
                </ul>
              </div>
            )}
          </div>
        )}
        {blocking.length > 0 && (
          <div className="callout error small">
            <ul>
              {blocking.map((b) => (
                <li key={b}>{b}</li>
              ))}
            </ul>
          </div>
        )}
        <ErrorMessage error={previewError ?? start.error} />
        <div className="row">
          <button type="button" className="primary" disabled={!canRun} onClick={() => request && start.mutate(request)}>
            {start.isPending ? 'Starting…' : 'Run comparison'}
          </button>
          <span className="small muted">Paid calls are made only when you press Run. Nothing is retried automatically.</span>
        </div>
      </section>
    </div>
  )
}
