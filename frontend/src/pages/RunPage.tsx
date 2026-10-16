import { useMutation, useQueryClient } from '@tanstack/react-query'
import { useEffect, useMemo, useRef, useState, type ReactNode } from 'react'
import { Link, useParams } from 'react-router'

import { newIdempotencyKey, post } from '../api/client'
import { keys, useComparison, useRun } from '../api/hooks'
import type { Attempt, RunDetail } from '../api/types'
import { ComparisonCharts } from '../components/charts/ComparisonCharts'
import { TotalBox } from '../components/cost'
import { ResponseCard } from '../components/ResponseCard'
import { Badge, Empty, ErrorMessage, Spinner, StatusBadge } from '../components/ui'
import { exportUrl } from '../lib/exportUrl'
import { formatDate, STATUS_LABELS } from '../lib/format'
import { formatMoney } from '../lib/money'

const RETRYABLE = new Set(['failed', 'cancelled', 'interrupted', 'skipped_budget'])

/** Latest attempt per (prompt, candidate, trial); earlier attempts are retries of it. */
export function latestAttempts(attempts: Attempt[]): Map<string, Attempt> {
  const latest = new Map<string, Attempt>()
  for (const attempt of attempts) {
    const key = `${attempt.prompt_id}|${attempt.candidate_id}|${attempt.trial_index}`
    const current = latest.get(key)
    if (!current || attempt.created_at >= current.created_at) latest.set(key, attempt)
  }
  return latest
}

export function RunHeader({ detail, actions }: { detail: RunDetail; actions?: ReactNode }) {
  const done = detail.attempts.filter((a) => !['queued', 'running'].includes(a.status)).length
  const total = detail.attempts.length
  return (
    <div className="stack">
      <div className="page-header">
        <div>
          <h1>{detail.run.title || 'Untitled run'}</h1>
          <p>
            {detail.run.kind === 'suite' ? 'Suite run' : detail.run.kind === 'smoke' ? 'Smoke test' : 'Comparison'} ·
            created {formatDate(detail.run.created_at)}
            {detail.run.trials > 1 ? ` · ${detail.run.trials} trials` : ''}
          </p>
        </div>
        <div className="row">
          <StatusBadge status={detail.run.status} />
          {actions}
        </div>
      </div>
      {total > 0 && (detail.active || done < total) && (
        <div className="stack-sm">
          <progress className="progress-bar" max={total} value={done} aria-label="Run progress" />
          <span className="small muted">
            {done} of {total} calls finished
          </span>
        </div>
      )}
      <div className="totals">
        <TotalBox title="Generation" total={detail.generation_cost} />
        <TotalBox title="Judging" total={detail.judging_cost} />
        <TotalBox title="Combined" total={detail.combined_cost} />
      </div>
      {detail.run.judge_error && <div className="callout warn small">{detail.run.judge_error}</div>}
      {Object.keys(detail.evaluation_counts ?? {}).length > 0 && (
        <p className="small muted" data-testid="evaluation-counts">
          Judge evaluations:{' '}
          {Object.entries(detail.evaluation_counts ?? {})
            .map(([status, count]) => `${count} ${STATUS_LABELS[status]?.toLowerCase() ?? status}`)
            .join(', ')}
        </p>
      )}
      {detail.budget && (
        <p className="small muted">
          Budget {formatMoney(detail.budget.limit)} (worst-case reservations are made before each call)
          {detail.budget.committed ? ` · committed ${formatMoney(detail.budget.committed)}` : ''}
        </p>
      )}
    </div>
  )
}

function ExportLinks({ runId }: { runId: string }) {
  const [includeText, setIncludeText] = useState(false)
  return (
    <div className="row" role="group" aria-label="Export">
      <label className="row small">
        <input type="checkbox" checked={includeText} onChange={(e) => setIncludeText(e.target.checked)} />
        Include prompt and answer text
      </label>
      <a className="btn" href={exportUrl(runId, 'json', includeText)} download>
        Download JSON
      </a>
      <a className="btn" href={exportUrl(runId, 'csv', includeText)} download>
        Download CSV
      </a>
    </div>
  )
}

export function RunPage() {
  const { runId = '' } = useParams()
  const query = useRun(runId)
  const queryClient = useQueryClient()
  const [promptIndex, setPromptIndex] = useState(0)
  const [trial, setTrial] = useState(1)
  const cancel = useMutation({
    mutationFn: () => post(`/api/runs/${runId}/cancel`),
    onSuccess: () => queryClient.invalidateQueries({ queryKey: keys.run(runId) }),
  })
  const retryKey = useRef(newIdempotencyKey())
  const retryFailed = useMutation({
    mutationFn: () => post(`/api/runs/${runId}/retry-failed`, {}, retryKey.current),
    onSuccess: () => {
      retryKey.current = newIdempotencyKey()
      queryClient.invalidateQueries({ queryKey: keys.run(runId) })
    },
  })
  const detail = query.data
  const wasActive = useRef(false)
  useEffect(() => {
    // When the run (and any judging) finishes, lists and comparisons that include it are out of date.
    if (!detail) return
    if (detail.active) wasActive.current = true
    else if (wasActive.current) {
      wasActive.current = false
      for (const key of ['runs', 'comparison', 'evaluations']) queryClient.invalidateQueries({ queryKey: [key] })
    }
  }, [detail, queryClient])
  const latest = useMemo(() => latestAttempts(detail?.attempts ?? []), [detail])
  const retriedIds = useMemo(
    () => new Set((detail?.attempts ?? []).map((a) => a.retry_of).filter(Boolean) as string[]),
    [detail],
  )

  if (query.isLoading) return <Spinner />
  if (query.error) return <ErrorMessage error={query.error} />
  if (!detail) return <Empty>Run not found.</Empty>

  const prompt = detail.prompts[Math.min(promptIndex, detail.prompts.length - 1)]
  const candidates = [...detail.candidates].sort((a, b) => a.position - b.position)
  // Billed responses that were not answers would most likely repeat; they are retried one at a time.
  const canRetry = [...latest.values()].some((a) => RETRYABLE.has(a.status) && a.error_type !== 'no_answer')

  return (
    <div className="stack">
      <RunHeader
        detail={detail}
        actions={
          <>
            {detail.active && (
              <button type="button" className="danger" onClick={() => cancel.mutate()} disabled={cancel.isPending}>
                Cancel run
              </button>
            )}
            {!detail.active && canRetry && (
              <button type="button" onClick={() => retryFailed.mutate()} disabled={retryFailed.isPending}>
                Retry failed
              </button>
            )}
            <Link to={`/scoring/${detail.run.id}`}>Score responses</Link>
            <ExportLinks runId={detail.run.id} />
          </>
        }
      />
      <ErrorMessage error={cancel.error ?? retryFailed.error} />

      {(detail.prompts.length > 1 || detail.run.trials > 1) && (
        <div className="row">
          {detail.prompts.length > 1 && (
            <label className="row">
              Prompt
              <select value={promptIndex} onChange={(e) => setPromptIndex(Number(e.target.value))}>
                {detail.prompts.map((p, index) => (
                  <option key={p.id} value={index}>
                    {index + 1}. {p.title || p.user_text.slice(0, 60)}
                  </option>
                ))}
              </select>
            </label>
          )}
          {detail.run.trials > 1 && (
            <label className="row">
              Trial
              <select value={trial} onChange={(e) => setTrial(Number(e.target.value))}>
                {Array.from({ length: detail.run.trials }, (_, i) => i + 1).map((t) => (
                  <option key={t} value={t}>
                    {t}
                  </option>
                ))}
              </select>
            </label>
          )}
        </div>
      )}

      {prompt && (
        <details className="card" open={detail.prompts.length === 1}>
          <summary>Exact prompt</summary>
          {prompt.system_text && (
            <>
              <h4>System instructions</h4>
              <div className="response-text">{prompt.system_text}</div>
            </>
          )}
          <h4>User prompt</h4>
          <div className="response-text">{prompt.user_text}</div>
        </details>
      )}

      <div className="cards" data-testid="response-cards">
        {prompt &&
          candidates.map((candidate) => {
            const attempt = latest.get(`${prompt.id}|${candidate.id}|${trial}`)
            if (!attempt) return null
            return (
              <ResponseCard
                key={attempt.id}
                runId={detail.run.id}
                attempt={attempt}
                candidate={candidate}
                manualScore={detail.manual_scores[attempt.id]}
                judgeScore={detail.judge_scores?.[attempt.id]}
                retried={retriedIds.has(attempt.id)}
              />
            )
          })}
      </div>
      {detail.attempts.some((a) => a.retry_of) && (
        <p className="small muted">
          <Badge tone="info">retry</Badge> Explicit retries are separate attempt records; earlier attempts stay in the run
          history and in cost totals.
        </p>
      )}
      {!detail.active && candidates.length >= 2 && <RunCharts runId={detail.run.id} />}
    </div>
  )
}

function RunCharts({ runId }: { runId: string }) {
  const comparison = useComparison([runId])
  if (!comparison.data) return comparison.error ? <ErrorMessage error={comparison.error} /> : null
  return <ComparisonCharts comparison={comparison.data} />
}
