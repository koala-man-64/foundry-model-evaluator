import { useMemo, useState } from 'react'
import { Link, useNavigate, useParams } from 'react-router'

import { useRun, useRuns } from '../api/hooks'
import { ManualScoreControl } from '../components/ResponseCard'
import { StatusBadge, Empty, ErrorMessage, Spinner } from '../components/ui'
import { JudgeSlot } from '../slots'
import { latestAttempts } from './RunPage'

export function ScoringPage() {
  const { runId } = useParams()
  const navigate = useNavigate()
  const runs = useRuns()
  const selected = runId ?? runs.data?.find((r) => r.run.status !== 'running')?.run.id
  const run = useRun(selected)
  const [promptIndex, setPromptIndex] = useState(0)
  const [trial, setTrial] = useState(1)
  const detail = run.data
  const latest = useMemo(() => latestAttempts(detail?.attempts ?? []), [detail])

  if (runs.isLoading) return <Spinner />
  if (!runs.data?.length) return <Empty>No runs to score yet.</Empty>
  const prompt = detail?.prompts[Math.min(promptIndex, (detail?.prompts.length ?? 1) - 1)]
  return (
    <div className="stack">
      <div className="row">
        <label className="row">
          Run
          <select value={selected} onChange={(e) => navigate(`/scoring/${e.target.value}`)}>
            {runs.data.map((r) => (
              <option key={r.run.id} value={r.run.id}>
                {r.run.title || 'Untitled run'} — {new Date(r.run.created_at).toLocaleString()}
              </option>
            ))}
          </select>
        </label>
        {detail && (detail.prompts.length > 1 || detail.run.trials > 1) && (
          <>
            <label className="row">
              Prompt
              <select value={promptIndex} onChange={(e) => setPromptIndex(Number(e.target.value))}>
                {detail.prompts.map((p, i) => (
                  <option key={p.id} value={i}>
                    {i + 1}. {p.title || p.user_text.slice(0, 50)}
                  </option>
                ))}
              </select>
            </label>
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
          </>
        )}
        {selected && <Link to={`/runs/${selected}`}>Open run</Link>}
      </div>
      <ErrorMessage error={run.error} />
      {detail && prompt && (
        <>
          <div className="card">
            <h3>Prompt</h3>
            {prompt.system_text && <div className="response-text small">{prompt.system_text}</div>}
            <div className="response-text">{prompt.user_text}</div>
          </div>
          <section aria-label="Manual scores" className="stack">
            <h2>Manual scores</h2>
            <p className="small muted">A 1–5 overall rating with optional notes, kept separately from judge scores.</p>
            <div className="cards">
              {detail.candidates.map((candidate) => {
                const attempt = latest.get(`${prompt.id}|${candidate.id}|${trial}`)
                if (!attempt) return null
                return (
                  <article key={attempt.id} className="card stack-sm" aria-label={`Score ${candidate.label}`}>
                    <div className="row-between">
                      <strong>{candidate.label}</strong>
                      <StatusBadge status={attempt.status} />
                    </div>
                    <div className="response-text">{attempt.output_text || attempt.error_message || '—'}</div>
                    {attempt.status === 'succeeded' && (
                      <ManualScoreControl runId={detail.run.id} attemptId={attempt.id} score={detail.manual_scores[attempt.id]} />
                    )}
                  </article>
                )
              })}
            </div>
          </section>
          <JudgeSlot detail={detail} promptId={prompt.id} trial={trial} />
        </>
      )}
    </div>
  )
}
