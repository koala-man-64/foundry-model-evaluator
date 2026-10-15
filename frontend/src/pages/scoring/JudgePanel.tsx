import { useMutation, useQueryClient } from '@tanstack/react-query'
import { useEffect, useMemo, useRef, useState } from 'react'

import { newIdempotencyKey, post } from '../../api/client'
import { keys, useEvaluations, useProfiles, useRubrics } from '../../api/hooks'
import type { CreatedEvaluations, GenerationSettings, JudgePreview, JudgeRequest, RunDetail } from '../../api/types'
import { useDebouncedPost } from '../../api/useDebouncedPost'
import { cleanSettings, SettingsEditor, unsupportedSettings } from '../../components/SettingsEditor'
import { Badge, Empty, ErrorMessage, Field } from '../../components/ui'
import { formatMoney } from '../../lib/money'
import { judgeDefaults, ProfileSelect } from '../suites/SuiteEditor'
import { EvaluationCard } from './EvaluationCard'

interface Props {
  detail: RunDetail
  promptId: string
  trial: number
}

export function JudgePanel({ detail, promptId, trial }: Props) {
  const queryClient = useQueryClient()
  const profilesQuery = useProfiles()
  const rubrics = useRubrics()
  const evaluations = useEvaluations(detail.run.id)
  const profiles = useMemo(() => profilesQuery.data ?? [], [profilesQuery.data])
  const [profileId, setProfileId] = useState('')
  const [rubricId, setRubricId] = useState('default')
  const [scope, setScope] = useState<'cell' | 'run'>('cell')
  const [settings, setSettings] = useState<GenerationSettings>({})
  const runKey = useRef(newIdempotencyKey())
  const profile = profiles.find((p) => p.id === profileId)

  useEffect(() => {
    if (!profileId && profiles.length) {
      // Prefer a judge that did not produce any of the answers.
      const participants = new Set(detail.candidates.map((c) => c.profile_id))
      const first = profiles.find((p) => !participants.has(p.id)) ?? profiles[0]
      setProfileId(first.id)
      setSettings(judgeDefaults(first))
    }
  }, [profiles, profileId, detail.candidates])

  const blocking = unsupportedSettings(profile?.capabilities, settings)
  const request: JudgeRequest | null = profileId
    ? {
        judge_profile_id: profileId,
        rubric_id: rubricId,
        settings: cleanSettings(settings),
        prompt_id: scope === 'cell' ? promptId : null,
        trial_index: scope === 'cell' ? trial : null,
      }
    : null
  const preview = useDebouncedPost<JudgePreview>(
    detail.active ? null : `/api/runs/${detail.run.id}/evaluations/preview`,
    request,
  )
  const start = useMutation({
    mutationFn: (body: JudgeRequest) =>
      post<CreatedEvaluations>(`/api/runs/${detail.run.id}/evaluations`, body, runKey.current),
    onSuccess: () => {
      runKey.current = newIdempotencyKey()
      queryClient.invalidateQueries({ queryKey: keys.evaluations(detail.run.id) })
      queryClient.invalidateQueries({ queryKey: keys.run(detail.run.id) })
    },
  })
  const shown = (evaluations.data ?? [])
    .filter((e) => e.prompt_id === promptId && e.trial_index === trial)
    .sort((a, b) => b.created_at.localeCompare(a.created_at))
  const data = preview.data
  const canRun = Boolean(request && data?.ok && blocking.length === 0 && !start.isPending && !detail.active)

  return (
    <section className="stack" aria-label="Judge">
      <h2>Judge</h2>
      <p className="small muted">
        A Foundry model scores the succeeded responses against a rubric, anonymized and in random order. Judge scores are kept separate
        from manual scores, and judging cost is reported separately from generation cost.
      </p>
      {detail.run.judge_error && <div className="callout warn small">{detail.run.judge_error}</div>}
      <div className="card stack">
        <div className="grid-form">
          <Field label="Judge model profile">
            <ProfileSelect
              profiles={profiles}
              label="Judge model profile"
              value={profileId}
              onChange={(id) => {
                setProfileId(id)
                setSettings((current) => ({ ...judgeDefaults(profiles.find((p) => p.id === id)), ...current }))
              }}
            />
          </Field>
          <Field label="Rubric">
            <select value={rubricId} onChange={(e) => setRubricId(e.target.value)}>
              {(rubrics.data ?? []).map((r) => (
                <option key={r.id} value={r.id}>
                  {r.name} (v{r.latest.version})
                </option>
              ))}
            </select>
          </Field>
          <Field label="Scope">
            <select value={scope} onChange={(e) => setScope(e.target.value as 'cell' | 'run')}>
              <option value="cell">This prompt and trial</option>
              <option value="run">Every prompt and trial of the run</option>
            </select>
          </Field>
        </div>
        {profile && (
          <details>
            <summary>Judge settings</summary>
            <SettingsEditor idPrefix="judge" capabilities={profile.capabilities} value={settings} onChange={setSettings} />
          </details>
        )}
        {detail.active && <p className="small muted">The run is still in progress; judge it when it finishes.</p>}
        {data && (
          <div className="stack-sm">
            <p className="small">
              {data.calls} judge call{data.calls === 1 ? '' : 's'} · worst-case estimated token cost{' '}
              {data.worst_case_total ? formatMoney(data.worst_case_total) : 'not bounded'} ·{' '}
              {data.structured_output ? 'structured JSON output' : 'JSON requested in the instructions'}
            </p>
            {data.groups.length > 0 && (
              <div className="table-wrap">
                <table className="compact" aria-label="Judge input size check">
                  <thead>
                    <tr>
                      <th>Prompt</th>
                      <th>Trial</th>
                      <th className="num">Responses</th>
                      <th className="num">Judge input (estimate)</th>
                      <th>Size</th>
                    </tr>
                  </thead>
                  <tbody>
                    {data.groups.map((g) => (
                      <tr key={`${g.prompt_id}-${g.trial_index}`}>
                        <td>{g.prompt_title}</td>
                        <td>{g.trial_index}</td>
                        <td className="num">{g.responses}</td>
                        <td className="num">
                          ≈ {g.input_estimate_tokens.toLocaleString('en-US')} of {g.input_limit_tokens.toLocaleString('en-US')}
                        </td>
                        <td>{g.oversized ? <Badge tone="warn">too large: will be rejected</Badge> : <Badge tone="ok">fits</Badge>}</td>
                      </tr>
                    ))}
                  </tbody>
                </table>
              </div>
            )}
            {data.errors.length > 0 && (
              <div className="callout error small" role="alert">
                <ul>
                  {data.errors.map((e) => (
                    <li key={e}>{e}</li>
                  ))}
                </ul>
              </div>
            )}
            {data.warnings.length > 0 && (
              <div className="callout warn small">
                <ul>
                  {data.warnings.map((w) => (
                    <li key={w}>{w}</li>
                  ))}
                </ul>
              </div>
            )}
          </div>
        )}
        {blocking.length > 0 && <div className="callout error small">{blocking.join(', ')}: set but not supported by the judge.</div>}
        <ErrorMessage error={preview.error ?? start.error} />
        <div className="row">
          <button type="button" className="primary" disabled={!canRun} onClick={() => request && start.mutate(request)}>
            {start.isPending ? 'Starting…' : 'Run judge'}
          </button>
          <span className="small muted">A paid call. Re-running creates a new evaluation; earlier ones are kept.</span>
        </div>
      </div>
      <ErrorMessage error={evaluations.error} />
      {shown.length === 0 && !evaluations.isLoading && <Empty>No evaluations for this prompt and trial yet.</Empty>}
      {shown.map((evaluation) => (
        <EvaluationCard key={evaluation.id} evaluation={evaluation} />
      ))}
    </section>
  )
}
