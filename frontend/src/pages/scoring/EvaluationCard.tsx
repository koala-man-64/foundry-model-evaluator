import { useMutation } from '@tanstack/react-query'
import { useState } from 'react'

import { get } from '../../api/client'
import type { EvaluationOut, EvaluationRaw } from '../../api/types'
import { CostValue } from '../../components/cost'
import { Badge, Dialog, ErrorMessage, JsonView, StatusBadge } from '../../components/ui'
import { FINISH_LABELS, formatDate, formatMs } from '../../lib/format'
import { SwapPass, swappable } from './SwapPass'

const LEFT_OUT =
  'Not used by comparisons: its prompt and trial have evaluations of more answers, or more (or more recent) ' +
  'evaluations with another judge, rubric version or settings, or an answer it judged was retried since. It stays ' +
  'in the history and in judging cost.'

interface Props {
  evaluation: EvaluationOut
  /** The run, to offer a swap pass from this evaluation (left out where no paid action belongs). */
  runId?: string
  /** Whether comparisons use this evaluation (undefined: not known here). */
  counted?: boolean
  swapDisabled?: boolean
}

export function EvaluationCard({ evaluation, runId, counted, swapDisabled }: Props) {
  const [raw, setRaw] = useState<EvaluationRaw | null>(null)
  const [show, setShow] = useState<'request' | 'response' | null>(null)
  const load = useMutation({
    mutationFn: () => get<EvaluationRaw>(`/api/evaluations/${evaluation.id}/raw`),
    onSuccess: setRaw,
  })
  const open = (which: 'request' | 'response') => {
    setShow(which)
    if (!raw) load.mutate()
  }
  const criteria = evaluation.rubric.definition.criteria
  const byLabel = new Map(evaluation.candidates.map((c) => [c.label, c]))
  const scale = `${evaluation.rubric.definition.scale_min}–${evaluation.rubric.definition.scale_max}`
  return (
    <article className="card stack-sm" aria-label={`Evaluation ${evaluation.id}`} data-testid="evaluation">
      <div className="card-header">
        <div className="stack-sm grow">
          <div className="card-title">
            Judge: {evaluation.judge_profile_name} · {evaluation.rubric_name} v{evaluation.rubric.version}
          </div>
          <div className="row small muted">
            <span>
              {evaluation.swap_of ? 'order swap of an earlier evaluation' : evaluation.trigger === 'auto' ? 'automatic (suite setting)' : 'run explicitly'}
            </span>
            <span>{formatDate(evaluation.created_at)}</span>
            <span>trial {evaluation.trial_index}</span>
            {evaluation.latency_ms !== null && evaluation.latency_ms !== undefined && <span>{formatMs(evaluation.latency_ms)}</span>}
          </div>
        </div>
        <div className="stack-sm right">
          <StatusBadge status={evaluation.status} />
          {evaluation.swap_of && <Badge tone="info">order swap</Badge>}
          {counted === false && evaluation.status === 'succeeded' && (
            <Badge title={LEFT_OUT}>not in comparisons</Badge>
          )}
          {evaluation.judge_is_participant && (
            <Badge tone="warn" title={evaluation.participant_reasons.join('; ')}>
              judge also took part
            </Badge>
          )}
        </div>
      </div>

      {evaluation.judge_is_participant && (
        <div className="callout warn small">The judge may be scoring its own output: {evaluation.participant_reasons.join('; ')}.</div>
      )}
      {evaluation.error_message && <div className="callout error small">{evaluation.error_message}</div>}
      {evaluation.parse_errors.length > 0 && (
        <div className="callout error small">
          <strong>Problems with the judge output</strong>
          <ul>
            {evaluation.parse_errors.map((e) => (
              <li key={e}>{e}</li>
            ))}
          </ul>
        </div>
      )}
      {evaluation.status === 'malformed' && evaluation.output_text !== null && evaluation.output_text !== undefined && (
        <details open>
          <summary>Raw judge output</summary>
          <pre className="response-text small">{evaluation.output_text}</pre>
        </details>
      )}

      {evaluation.status === 'succeeded' && (
        <>
          <p className="small muted">
            Responses were shown to the judge as {evaluation.order.map((o) => o.label).join(', ')}{' '}
            {evaluation.swap_of ? 'in the reverse of the order of the evaluation it swaps' : 'in this random order'}, without model
            names, costs or timing. Revealed below. Scores are whole numbers from {scale}.
          </p>
          <div className="table-wrap">
            <table className="compact">
              <thead>
                <tr>
                  <th>Label</th>
                  <th>Candidate (revealed)</th>
                  {criteria.map((c) => (
                    <th key={c.key} className="num" title={c.description}>
                      {c.name}
                    </th>
                  ))}
                  <th className="num">Judge overall</th>
                  <th className="num">Weighted overall</th>
                </tr>
              </thead>
              <tbody>
                {evaluation.order.map((entry) => {
                  const result = byLabel.get(entry.label)
                  const scores = new Map((result?.criteria ?? []).map((c) => [c.key, c]))
                  return (
                    <tr key={entry.label} data-testid={`evaluation-row-${entry.label}`}>
                      <td>
                        <strong>{entry.label}</strong>{' '}
                        {evaluation.preferred === entry.label && <Badge tone="ok">preferred</Badge>}
                      </td>
                      <td>{evaluation.candidate_labels[entry.candidate_id] ?? entry.candidate_id}</td>
                      {criteria.map((c) => (
                        <td key={c.key} className="num" title={scores.get(c.key)?.explanation}>
                          {scores.get(c.key)?.score ?? '—'}
                        </td>
                      ))}
                      <td className="num">{result ? result.judge_overall.toFixed(2) : '—'}</td>
                      <td className="num">{result ? result.weighted_overall.toFixed(2) : '—'}</td>
                    </tr>
                  )
                })}
              </tbody>
            </table>
          </div>
          <p className="small">
            {evaluation.preferred === 'tie' ? <strong>Judge preference: tie.</strong> : <strong>Judge preference: {evaluation.preferred}.</strong>}{' '}
            {evaluation.preference_explanation}
          </p>
          <details>
            <summary>Explanations</summary>
            {evaluation.candidates.map((c) => (
              <div key={c.label} className="stack-sm">
                <h4>
                  Response {c.label} — {evaluation.candidate_labels[c.candidate_id]}
                </h4>
                <p className="small">{c.explanation}</p>
                <ul className="small">
                  {c.criteria.map((score) => (
                    <li key={score.key}>
                      {criteria.find((k) => k.key === score.key)?.name ?? score.key}: {score.score} — {score.explanation}
                    </li>
                  ))}
                </ul>
              </div>
            ))}
          </details>
        </>
      )}

      <div className="row-between section">
        <span className="small">
          <span className="cost-label">Estimated token cost (judging)</span> <CostValue cost={evaluation.cost} />
        </span>
        <span className="small muted">
          Judge input ≈ {evaluation.input_estimate_tokens.toLocaleString('en-US')} of {evaluation.input_limit_tokens.toLocaleString('en-US')}{' '}
          tokens (estimate)
          {evaluation.finish_reason && evaluation.finish_reason !== 'completed'
            ? ` · ${FINISH_LABELS[evaluation.finish_reason] ?? evaluation.finish_reason}`
            : ''}
        </span>
      </div>
      <div className="row">
        <button type="button" className="small" onClick={() => open('request')}>
          Judge request
        </button>
        <button type="button" className="small" onClick={() => open('response')} disabled={!evaluation.request_sent_at}>
          Raw judge response
        </button>
        {runId && swappable(evaluation) && <SwapPass runId={runId} evaluation={evaluation} disabled={swapDisabled} />}
      </div>
      <ErrorMessage error={load.error} />
      <Dialog open={show === 'request'} title="Exact judge request" onClose={() => setShow(null)}>
        <p className="small muted">This is everything the judge received. Model names, costs and timing are never included.</p>
        <JsonView value={raw?.request_body ?? null} />
      </Dialog>
      <Dialog open={show === 'response'} title="Raw judge response" onClose={() => setShow(null)}>
        <h3>Original usage object</h3>
        <JsonView value={raw?.raw_usage ?? null} />
        <h3>Response</h3>
        <JsonView value={raw?.raw_response ?? null} />
      </Dialog>
    </article>
  )
}
