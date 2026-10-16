import { useMutation, useQueryClient } from '@tanstack/react-query'
import { useRef, useState } from 'react'

import { del, get, newIdempotencyKey, post, put } from '../api/client'
import { keys } from '../api/hooks'
import type { Attempt, CandidateOut, JudgeScore, ManualScore, RawOut } from '../api/types'
import { FINISH_LABELS } from '../lib/format'
import { CostDetails, CostValue } from './cost'
import { MarkdownView } from './MarkdownView'
import { Badge, Dialog, ErrorMessage, JsonView, StatusBadge } from './ui'
import { TimingTable, UsageTable } from './usage'

const RETRYABLE = new Set(['failed', 'cancelled', 'interrupted', 'skipped_budget'])

interface Props {
  runId: string
  attempt: Attempt
  candidate: CandidateOut | undefined
  manualScore?: ManualScore
  judgeScore?: JudgeScore
  retried: boolean
}

/** The latest judge result for a response (separate from the manual score). */
export function JudgeScoreLine({ score }: { score: JudgeScore }) {
  return (
    <div className="row small" data-testid="judge-score">
      <span className="muted">Judge score</span>
      <strong>{score.weighted_overall.toFixed(2)}</strong>
      <span className="muted">(weighted; judge overall {score.judge_overall.toFixed(2)}, label {score.label})</span>
      {score.preferred && <Badge tone="ok">preferred</Badge>}
      {score.tie && <Badge>tie</Badge>}
      {score.judge_is_participant && <Badge tone="warn">judge also took part</Badge>}
    </div>
  )
}

export function ManualScoreControl({ runId, attemptId, score }: { runId: string; attemptId: string; score?: ManualScore }) {
  const queryClient = useQueryClient()
  const [notes, setNotes] = useState(score?.notes ?? '')
  // Manual scores feed the run view and the judge calibration.
  const refresh = () =>
    Promise.all([
      queryClient.invalidateQueries({ queryKey: keys.run(runId) }),
      queryClient.invalidateQueries({ queryKey: ['calibration'] }),
    ])
  const save = useMutation({
    mutationFn: (value: number) => put<ManualScore>(`/api/attempts/${attemptId}/manual-score`, { score: value, notes }),
    onSuccess: () => refresh(),
  })
  const clear = useMutation({
    mutationFn: () => del(`/api/attempts/${attemptId}/manual-score`),
    onSuccess: () => refresh(),
  })
  return (
    <div className="stack-sm">
      <div className="row">
        <span className="small muted">Manual score</span>
        <div className="score-buttons" role="group" aria-label="Manual score (1 to 5)">
          {[1, 2, 3, 4, 5].map((value) => (
            <button
              key={value}
              type="button"
              aria-pressed={score?.score === value}
              aria-label={`Score ${value}`}
              disabled={save.isPending}
              onClick={() => save.mutate(value)}
            >
              {value}
            </button>
          ))}
        </div>
        {score && (
          <button type="button" className="ghost small" onClick={() => clear.mutate()}>
            Clear
          </button>
        )}
      </div>
      <textarea
        aria-label="Manual score notes"
        placeholder="Notes (optional, saved with the score)"
        value={notes}
        rows={2}
        onChange={(e) => setNotes(e.target.value)}
        onBlur={() => {
          if (score && notes !== score.notes) save.mutate(score.score)
        }}
      />
      <ErrorMessage error={save.error ?? clear.error} />
    </div>
  )
}

export function ResponseCard({ runId, attempt, candidate, manualScore, judgeScore, retried }: Props) {
  const queryClient = useQueryClient()
  const [markdown, setMarkdown] = useState(false)
  const [showRequest, setShowRequest] = useState(false)
  const [raw, setRaw] = useState<RawOut | null>(null)
  // One key per retry action: a repeated click before the first answer cannot pay twice.
  const retryKey = useRef(newIdempotencyKey())
  const retry = useMutation({
    mutationFn: () => post(`/api/attempts/${attempt.id}/retry`, {}, retryKey.current),
    onSuccess: () => {
      retryKey.current = newIdempotencyKey()
      queryClient.invalidateQueries({ queryKey: keys.run(runId) })
    },
  })
  const loadRaw = useMutation({
    mutationFn: () => get<RawOut>(`/api/attempts/${attempt.id}/raw`),
    onSuccess: setRaw,
  })
  const done = !['queued', 'running'].includes(attempt.status)
  return (
    <article className="card stack-sm" aria-label={`Response from ${candidate?.label ?? 'candidate'}`}>
      <div className="card-header">
        <div className="stack-sm grow">
          <div className="card-title">{candidate?.label ?? 'Candidate'}</div>
          <div className="row small muted">
            <span>{candidate?.deployment}</span>
            {attempt.trial_index > 1 && <span>trial {attempt.trial_index}</span>}
            {attempt.retry_of && <Badge tone="info">retry</Badge>}
          </div>
        </div>
        <div className="stack-sm right">
          <StatusBadge status={attempt.status} />
          {attempt.finish_reason && attempt.finish_reason !== 'completed' && (
            <Badge tone={attempt.finish_reason === 'error' ? 'error' : 'warn'}>
              {FINISH_LABELS[attempt.finish_reason] ?? attempt.finish_reason}
            </Badge>
          )}
        </div>
      </div>

      {attempt.status === 'running' || attempt.status === 'queued' ? (
        <p className="muted">
          <span className="spinner" /> {attempt.status === 'queued' ? 'Waiting for a free slot…' : 'Waiting for the response…'}
        </p>
      ) : (
        <>
          {attempt.error_message && <div className="callout error small">{attempt.error_message}</div>}
          {attempt.output_text ? (
            <>
              <div className="row-between">
                <span className="small muted">Answer</span>
                <div className="button-group" role="group" aria-label="Answer format">
                  <button type="button" className="small" aria-pressed={!markdown} onClick={() => setMarkdown(false)}>
                    Text
                  </button>
                  <button type="button" className="small" aria-pressed={markdown} onClick={() => setMarkdown(true)}>
                    Markdown
                  </button>
                </div>
              </div>
              {markdown ? (
                <MarkdownView text={attempt.output_text} />
              ) : (
                <div className="response-text" data-testid="response-text">
                  {attempt.output_text}
                </div>
              )}
            </>
          ) : null}
          {attempt.reasoning_summary && (
            <details>
              <summary>Reasoning summary</summary>
              <div className="response-text small">{attempt.reasoning_summary}</div>
            </details>
          )}
        </>
      )}

      <div className="section">
        <UsageTable usage={attempt.usage} cost={attempt.cost} />
      </div>
      <div className="section">
        <TimingTable attempt={attempt} />
      </div>
      <div className="section stack-sm">
        <div className="row-between">
          <span className="cost-label">Estimated token cost</span>
          <CostValue cost={attempt.cost} />
        </div>
        <CostDetails cost={attempt.cost} />
      </div>
      {done && (
        <div className="section stack-sm">
          <ManualScoreControl runId={runId} attemptId={attempt.id} score={manualScore} />
          {judgeScore && <JudgeScoreLine score={judgeScore} />}
        </div>
      )}
      <div className="row section">
        <button type="button" className="small" onClick={() => setShowRequest(true)}>
          Request
        </button>
        <button type="button" className="small" onClick={() => loadRaw.mutate()} disabled={!done}>
          Raw response
        </button>
        {RETRYABLE.has(attempt.status) && !retried && (
          <button type="button" className="small" onClick={() => retry.mutate()} disabled={retry.isPending}>
            Retry
          </button>
        )}
      </div>
      <ErrorMessage error={retry.error ?? loadRaw.error} />

      <Dialog open={showRequest} title="Exact request and effective settings" onClose={() => setShowRequest(false)}>
        <h3>Requested settings</h3>
        <JsonView value={candidate?.settings ?? {}} />
        <p className="small muted">Settings that are not shown were omitted, so the provider default applied.</p>
        <h3>Provider request body</h3>
        <JsonView value={attempt.request_body} />
      </Dialog>
      <Dialog open={raw !== null} title="Raw provider response" onClose={() => setRaw(null)}>
        <h3>Original usage object</h3>
        <JsonView value={raw?.raw_usage ?? null} />
        <h3>Response</h3>
        <JsonView value={raw?.raw_response ?? null} />
      </Dialog>
    </article>
  )
}
