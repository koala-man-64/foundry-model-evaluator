import { type KeyboardEvent, useMemo, useState } from 'react'
import { Link, useNavigate, useParams } from 'react-router'

import { useRun, useRuns } from '../api/hooks'
import type { Attempt } from '../api/types'
import { JudgeScoreLine, ManualScoreControl } from '../components/ResponseCard'
import { StatusBadge, Empty, ErrorMessage, Spinner } from '../components/ui'
import { latestAttempts } from './RunPage'
import { CalibrationPanel } from './scoring/CalibrationPanel'
import { JudgePanel } from './scoring/JudgePanel'
import { BLIND_LABELS, blindOrder, nextUnscoredCell } from './scoring/queue'

function typing(target: EventTarget): boolean {
  return target instanceof HTMLTextAreaElement || target instanceof HTMLInputElement || target instanceof HTMLSelectElement
}

/** 1–5 on a focused answer scores it, as if its score button were pressed. */
function scoreHotkey(event: KeyboardEvent<HTMLElement>) {
  if (typing(event.target) || !/^[1-5]$/.test(event.key)) return
  const button = event.currentTarget.querySelector<HTMLButtonElement>(`button[aria-label="Score ${event.key}"]`)
  if (button) {
    event.preventDefault()
    button.click()
  }
}

export function ScoringPage() {
  const { runId } = useParams()
  const navigate = useNavigate()
  const runs = useRuns()
  const selected = runId ?? runs.data?.find((r) => r.run.status !== 'running')?.run.id
  const run = useRun(selected)
  const [promptIndex, setPromptIndex] = useState(0)
  const [trial, setTrial] = useState(1)
  const [blind, setBlind] = useState(false)
  const detail = run.data
  const latest = useMemo(() => latestAttempts(detail?.attempts ?? []), [detail])

  if (runs.isLoading) return <Spinner />
  if (!runs.data?.length) return <Empty>No runs to score yet.</Empty>
  const currentIndex = Math.min(promptIndex, (detail?.prompts.length ?? 1) - 1)
  const prompt = detail?.prompts[currentIndex]
  const next = detail ? nextUnscoredCell(detail, latest, { promptIndex: currentIndex, trial }) : null
  const goNext = () => {
    if (!next) return
    setPromptIndex(next.promptIndex)
    setTrial(next.trial)
  }
  const cards: { attempt: Attempt; label: string }[] = []
  if (detail && prompt) {
    for (const candidate of detail.candidates) {
      const attempt = latest.get(`${prompt.id}|${candidate.id}|${trial}`)
      if (attempt) cards.push({ attempt, label: candidate.label })
    }
  }
  const shown = blind ? blindOrder(cards.map((c) => ({ ...c, id: c.attempt.id }))) : cards
  return (
    <div
      className="stack"
      onKeyDown={(event) => {
        if (!typing(event.target) && event.key.toLowerCase() === 'n' && next) goNext()
      }}
    >
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
              <select value={currentIndex} onChange={(e) => setPromptIndex(Number(e.target.value))}>
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
        {detail && (
          <button type="button" onClick={goNext} disabled={!next} title="Keyboard: N">
            {next ? 'Next unscored' : 'All answers scored'}
          </button>
        )}
        <label className="row small">
          <input type="checkbox" checked={blind} onChange={(e) => setBlind(e.target.checked)} />
          Blind review
        </label>
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
            <p className="small muted">
              A 1–5 overall rating with optional notes, kept separately from judge scores. Select an answer and press 1–5 to score it;
              press N for the next unscored answer.
              {blind && ' Blind review hides the model names and judge scores and shuffles the answers.'}
            </p>
            <div className="cards">
              {shown.map(({ attempt, label }, position) => {
                const name = blind ? `Response ${BLIND_LABELS[position] ?? position + 1}` : label
                return (
                  <article
                    key={attempt.id}
                    className="card stack-sm"
                    aria-label={`Score ${name}`}
                    tabIndex={0}
                    onKeyDown={scoreHotkey}
                  >
                    <div className="row-between">
                      <strong>{name}</strong>
                      <StatusBadge status={attempt.status} />
                    </div>
                    <div className="response-text">{attempt.output_text || attempt.error_message || '—'}</div>
                    {!blind && detail.judge_scores?.[attempt.id] && <JudgeScoreLine score={detail.judge_scores[attempt.id]} />}
                    {attempt.status === 'succeeded' && (
                      <ManualScoreControl runId={detail.run.id} attemptId={attempt.id} score={detail.manual_scores[attempt.id]} />
                    )}
                  </article>
                )
              })}
            </div>
          </section>
          {!blind && <JudgePanel detail={detail} promptId={prompt.id} trial={trial} />}
          <CalibrationPanel runIds={[detail.run.id]} />
        </>
      )}
    </div>
  )
}
