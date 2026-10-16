import { useMutation, useQueryClient } from '@tanstack/react-query'
import { useRef, useState, type ChangeEvent, type FormEvent, type ReactNode } from 'react'

import { post, put } from '../../api/client'
import { useProfiles, useRubrics } from '../../api/hooks'
import type { GenerationSettings, ModelProfile, Suite, SuiteIn } from '../../api/types'
import { type CheckDraft, ChecksEditor, checkToDraft, draftToCheck } from '../../components/ChecksEditor'
import { cleanSettings, SettingsEditor, unsupportedSettings } from '../../components/SettingsEditor'
import { ErrorMessage, Field, Spinner } from '../../components/ui'
import { API_STYLE_LABELS } from '../../lib/format'
import { importPrompts } from '../../lib/importPrompts'

interface PromptDraft {
  key: string
  id: string | null
  title: string
  system: string
  user: string
  reference: string
  checks: CheckDraft[]
}

function blankPrompt(): PromptDraft {
  return { key: nextKey(), id: null, title: '', system: '', user: '', reference: '', checks: [] }
}

function isBlank(prompt: PromptDraft): boolean {
  return !prompt.id && !prompt.title && !prompt.system && !prompt.user.trim() && !prompt.reference && !prompt.checks.length
}

/** A details panel that starts open when it has content and then stays as the user leaves it. */
function Disclosure({ initiallyOpen, summary, children }: { initiallyOpen: boolean; summary: ReactNode; children: ReactNode }) {
  const [open, setOpen] = useState(initiallyOpen)
  return (
    <details open={open} onToggle={(e) => setOpen(e.currentTarget.open)}>
      <summary className="small">{summary}</summary>
      {children}
    </details>
  )
}

interface CandidateDraft {
  key: string
  id: string | null
  profileId: string
  label: string
  settings: GenerationSettings
}

interface JudgeDraft {
  enabled: boolean
  profileId: string
  rubricId: string
  settings: GenerationSettings
}

let counter = 0
const nextKey = () => {
  counter += 1
  return `k${Date.now()}-${counter}`
}

export function requiredDefaults(profile: ModelProfile | undefined): GenerationSettings {
  const spec = profile?.capabilities.settings?.max_output_tokens
  if (spec?.supported && spec.required) return { max_output_tokens: Math.min(4096, spec.maximum ?? 4096) }
  return {}
}

export function judgeDefaults(profile: ModelProfile | undefined): GenerationSettings {
  const spec = profile?.capabilities.settings?.max_output_tokens
  return spec?.supported ? { max_output_tokens: Math.min(4096, spec.maximum ?? 4096) } : {}
}

export function ProfileSelect({
  profiles,
  value,
  onChange,
  label,
  id,
  'aria-describedby': describedBy,
}: {
  profiles: ModelProfile[]
  value: string
  onChange: (id: string) => void
  label: string
  id?: string
  'aria-describedby'?: string
}) {
  return (
    <select id={id} aria-describedby={describedBy} aria-label={label} value={value} onChange={(e) => onChange(e.target.value)}>
      <option value="">Choose a model profile…</option>
      {profiles.map((p) => (
        <option key={p.id} value={p.id}>
          {p.name} — {API_STYLE_LABELS[p.api_style] ?? p.api_style}
        </option>
      ))}
    </select>
  )
}

export function SuiteEditor({ suite, onSaved, onCancel }: { suite?: Suite; onSaved: (suite: Suite) => void; onCancel: () => void }) {
  const queryClient = useQueryClient()
  const profilesQuery = useProfiles()
  const rubrics = useRubrics()
  const profiles = profilesQuery.data ?? []
  const byId = new Map(profiles.map((p) => [p.id, p]))
  const [name, setName] = useState(suite?.name ?? '')
  const [description, setDescription] = useState(suite?.description ?? '')
  const [trials, setTrials] = useState(suite?.trials ?? 1)
  const [prompts, setPrompts] = useState<PromptDraft[]>(
    suite?.prompts.map((p) => ({
      key: p.id,
      id: p.id,
      title: p.title,
      system: p.system,
      user: p.user,
      reference: p.reference ?? '',
      checks: (p.checks ?? []).map(checkToDraft),
    })) ?? [blankPrompt()],
  )
  const fileInput = useRef<HTMLInputElement>(null)
  const [importNote, setImportNote] = useState<string[]>([])
  const onImport = async (event: ChangeEvent<HTMLInputElement>) => {
    const file = event.target.files?.[0]
    event.target.value = ''
    if (!file) return
    // Trailing prompts with nothing typed in them are replaced, not kept in front of the imported ones.
    const kept = [...prompts]
    while (kept.length && isBlank(kept[kept.length - 1])) kept.pop()
    const result = importPrompts(file.name, await file.text(), kept.length)
    const added = result.prompts.map((p) => ({ ...blankPrompt(), ...p, checks: p.checks.map(checkToDraft) }))
    if (added.length) setPrompts([...kept, ...added])
    setImportNote([`Imported ${added.length} prompt${added.length === 1 ? '' : 's'} from ${file.name}.`, ...result.errors])
  }
  const [candidates, setCandidates] = useState<CandidateDraft[]>(
    suite?.candidates.map((c) => ({
      key: c.id,
      id: c.id,
      profileId: c.profile_id,
      label: c.label ?? '',
      settings: c.settings,
    })) ?? [{ key: nextKey(), id: null, profileId: '', label: '', settings: {} }],
  )
  const [judge, setJudge] = useState<JudgeDraft>({
    enabled: Boolean(suite?.judge?.enabled),
    profileId: suite?.judge?.judge_profile_id ?? '',
    rubricId: suite?.judge?.rubric_id ?? 'default',
    settings: suite?.judge?.settings ?? {},
  })

  const save = useMutation({
    mutationFn: (body: SuiteIn) => (suite ? put<Suite>(`/api/suites/${suite.id}`, body) : post<Suite>('/api/suites', body)),
    onSuccess: (saved) => {
      queryClient.invalidateQueries({ queryKey: ['suites'] })
      queryClient.invalidateQueries({ queryKey: ['suite', saved.id] })
      queryClient.invalidateQueries({ queryKey: ['suite-prompts'] })
      onSaved(saved)
    },
  })

  if (profilesQuery.isLoading || rubrics.isLoading) return <Spinner />

  const problems: string[] = []
  if (!name.trim()) problems.push('Give the suite a name.')
  if (prompts.some((p) => !p.user.trim())) problems.push('Every prompt needs a user prompt.')
  prompts.forEach((p, index) =>
    p.checks.forEach((check, checkIndex) => {
      const result = draftToCheck(check)
      if (typeof result === 'string') problems.push(`Prompt ${index + 1}, check ${checkIndex + 1}: ${result}.`)
    }),
  )
  candidates.forEach((c, index) => {
    if (!c.profileId) problems.push(`Candidate ${index + 1}: choose a model profile.`)
    for (const label of unsupportedSettings(byId.get(c.profileId)?.capabilities, c.settings))
      problems.push(`Candidate ${index + 1}: ${label} is set but not supported.`)
  })
  if (judge.enabled) {
    if (!judge.profileId) problems.push('Automatic judging: choose a judge model profile.')
    for (const label of unsupportedSettings(byId.get(judge.profileId)?.capabilities, judge.settings))
      problems.push(`Judge: ${label} is set but not supported.`)
  }

  const submit = (event: FormEvent) => {
    event.preventDefault()
    if (problems.length) return
    save.mutate({
      name: name.trim(),
      description,
      trials,
      prompts: prompts.map((p) => ({
        id: p.id,
        title: p.title,
        system: p.system,
        user: p.user,
        reference: p.reference,
        checks: p.checks.map(draftToCheck).filter((c) => typeof c !== 'string'),
      })),
      candidates: candidates.map((c) => ({
        id: c.id,
        profile_id: c.profileId,
        label: c.label.trim() || null,
        settings: cleanSettings(c.settings),
      })),
      judge: judge.enabled
        ? { enabled: true, judge_profile_id: judge.profileId, rubric_id: judge.rubricId, settings: cleanSettings(judge.settings) }
        : null,
    })
  }

  const updatePrompt = (key: string, patch: Partial<PromptDraft>) =>
    setPrompts((list) => list.map((p) => (p.key === key ? { ...p, ...patch } : p)))
  const updateCandidate = (key: string, patch: Partial<CandidateDraft>) =>
    setCandidates((list) => list.map((c) => (c.key === key ? { ...c, ...patch } : c)))

  return (
    <form className="stack" onSubmit={submit} aria-label={suite ? 'Edit suite' : 'New suite'}>
      <section className="card stack">
        <h2>{suite ? `Edit ${suite.name}` : 'New suite'}</h2>
        <div className="grid-form">
          <Field label="Suite name">
            <input value={name} onChange={(e) => setName(e.target.value)} />
          </Field>
          <Field label="Trials per prompt" hint="Each prompt is sent this many times to every candidate.">
            <input
              type="number"
              min={1}
              max={50}
              value={trials}
              onChange={(e) => setTrials(Math.min(50, Math.max(1, Number(e.target.value) || 1)))}
            />
          </Field>
        </div>
        <Field label="Description (optional)">
          <textarea rows={2} value={description} onChange={(e) => setDescription(e.target.value)} />
        </Field>
      </section>

      <section className="stack" aria-label="Suite prompts">
        <div className="row-between">
          <h2>Prompts</h2>
          <div className="row">
            <button type="button" onClick={() => fileInput.current?.click()} title="CSV with a user column, or JSON lines">
              Import prompts
            </button>
            <input
              ref={fileInput}
              type="file"
              accept=".csv,.jsonl,.json,.ndjson"
              hidden
              aria-label="Import prompts from a file"
              onChange={onImport}
            />
            <button type="button" onClick={() => setPrompts((list) => [...list, blankPrompt()])}>
              Add prompt
            </button>
          </div>
        </div>
        {importNote.length > 0 && (
          <div className="callout small" data-testid="import-note">
            {importNote.map((line) => (
              <div key={line}>{line}</div>
            ))}
          </div>
        )}
        {prompts.map((prompt, index) => (
          <div key={prompt.key} className="card stack-sm" data-testid="suite-prompt">
            <div className="row-between">
              <strong>Prompt {index + 1}</strong>
              {prompts.length > 1 && (
                <button
                  type="button"
                  className="small danger"
                  onClick={() => setPrompts((list) => list.filter((p) => p.key !== prompt.key))}
                >
                  Remove prompt
                </button>
              )}
            </div>
            <Field label={`Prompt ${index + 1} title (optional)`}>
              <input value={prompt.title} onChange={(e) => updatePrompt(prompt.key, { title: e.target.value })} />
            </Field>
            <Field label={`Prompt ${index + 1} system instructions (optional)`}>
              <textarea rows={2} value={prompt.system} onChange={(e) => updatePrompt(prompt.key, { system: e.target.value })} />
            </Field>
            <Field label={`Prompt ${index + 1} user prompt`}>
              <textarea rows={3} value={prompt.user} onChange={(e) => updatePrompt(prompt.key, { user: e.target.value })} />
            </Field>
            <Disclosure
              initiallyOpen={Boolean(prompt.reference || prompt.checks.length)}
              summary={`Reference answer and checks${
                prompt.checks.length > 0 ? ` (${prompt.checks.length} check${prompt.checks.length === 1 ? '' : 's'})` : ''
              }`}
            >
              <div className="stack-sm">
                <Field
                  label={`Prompt ${index + 1} reference answer (optional)`}
                  hint="Shown to the judge to grade correctness. It may be imperfect; the judge is told so."
                >
                  <textarea
                    rows={2}
                    value={prompt.reference}
                    onChange={(e) => updatePrompt(prompt.key, { reference: e.target.value })}
                  />
                </Field>
                <p className="small muted">
                  Checks run on every answer for free. An answer passes when every check passes; the pass rate is shown
                  next to cost, separately from manual and judge scores.
                </p>
                <ChecksEditor
                  label={`Prompt ${index + 1} check`}
                  checks={prompt.checks}
                  onChange={(checks) => updatePrompt(prompt.key, { checks })}
                />
              </div>
            </Disclosure>
          </div>
        ))}
      </section>

      <section className="stack" aria-label="Suite candidates">
        <div className="row-between">
          <h2>Models and settings</h2>
          <button
            type="button"
            onClick={() => setCandidates((list) => [...list, { key: nextKey(), id: null, profileId: '', label: '', settings: {} }])}
          >
            Add candidate
          </button>
        </div>
        {candidates.map((candidate, index) => {
          const profile = byId.get(candidate.profileId)
          return (
            <div key={candidate.key} className="card stack-sm" data-testid="suite-candidate">
              <div className="row-between">
                <div className="row grow">
                  <Field label={`Candidate ${index + 1}`}>
                    <ProfileSelect
                      profiles={profiles}
                      label={`Model profile for suite candidate ${index + 1}`}
                      value={candidate.profileId}
                      onChange={(id) =>
                        updateCandidate(candidate.key, {
                          profileId: id,
                          settings: { ...requiredDefaults(byId.get(id)), ...candidate.settings },
                        })
                      }
                    />
                  </Field>
                  <Field label="Label (optional)">
                    <input value={candidate.label} onChange={(e) => updateCandidate(candidate.key, { label: e.target.value })} />
                  </Field>
                </div>
                <div className="row">
                  <button
                    type="button"
                    className="small"
                    onClick={() =>
                      setCandidates((list) => {
                        const copy = { ...candidate, key: nextKey(), id: null, label: '', settings: { ...candidate.settings } }
                        const next = [...list]
                        next.splice(index + 1, 0, copy)
                        return next
                      })
                    }
                  >
                    Duplicate
                  </button>
                  {candidates.length > 1 && (
                    <button
                      type="button"
                      className="small danger"
                      onClick={() => setCandidates((list) => list.filter((c) => c.key !== candidate.key))}
                    >
                      Remove
                    </button>
                  )}
                </div>
              </div>
              {profile && (
                <details open>
                  <summary>Generation settings</summary>
                  <SettingsEditor
                    idPrefix={`suite-${candidate.key}`}
                    capabilities={profile.capabilities}
                    value={candidate.settings}
                    onChange={(settings) => updateCandidate(candidate.key, { settings })}
                  />
                </details>
              )}
            </div>
          )
        })}
      </section>

      <section className="card stack" aria-label="Automatic judging">
        <label className="checkbox">
          <input
            type="checkbox"
            checked={judge.enabled}
            onChange={(e) => setJudge((j) => ({ ...j, enabled: e.target.checked }))}
          />
          Judge every run of this suite automatically (paid judge calls after the responses finish)
        </label>
        {judge.enabled && (
          <>
            <div className="grid-form">
              <Field label="Judge model profile">
                <ProfileSelect
                  profiles={profiles}
                  label="Judge model profile"
                  value={judge.profileId}
                  onChange={(id) => setJudge((j) => ({ ...j, profileId: id, settings: { ...judgeDefaults(byId.get(id)), ...j.settings } }))}
                />
              </Field>
              <Field label="Rubric" hint="Each run uses the rubric's current version, frozen when the run starts.">
                <select value={judge.rubricId} onChange={(e) => setJudge((j) => ({ ...j, rubricId: e.target.value }))}>
                  {(rubrics.data ?? []).map((r) => (
                    <option key={r.id} value={r.id}>
                      {r.name} (v{r.latest.version})
                    </option>
                  ))}
                </select>
              </Field>
            </div>
            {byId.get(judge.profileId) && (
              <details>
                <summary>Judge settings</summary>
                <SettingsEditor
                  idPrefix="suite-judge"
                  capabilities={byId.get(judge.profileId)?.capabilities}
                  value={judge.settings}
                  onChange={(settings) => setJudge((j) => ({ ...j, settings }))}
                />
              </details>
            )}
          </>
        )}
      </section>

      {problems.length > 0 && (
        <div className="callout warn small">
          <ul>
            {problems.map((p) => (
              <li key={p}>{p}</li>
            ))}
          </ul>
        </div>
      )}
      <ErrorMessage error={save.error} />
      <div className="row">
        <button type="submit" className="primary" disabled={save.isPending || problems.length > 0}>
          Save suite
        </button>
        <button type="button" onClick={onCancel}>
          Cancel
        </button>
      </div>
    </form>
  )
}
