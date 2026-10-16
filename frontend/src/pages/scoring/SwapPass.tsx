import { useMutation, useQueryClient } from '@tanstack/react-query'
import { useRef, useState } from 'react'

import { newIdempotencyKey, post } from '../../api/client'
import { keys } from '../../api/hooks'
import type { CreatedEvaluations, EvaluationOut, JudgePreview, SwapRequest } from '../../api/types'
import { useDebouncedPost } from '../../api/useDebouncedPost'
import { ErrorMessage } from '../../components/ui'
import { formatMoney } from '../../lib/money'

type Scope = NonNullable<SwapRequest['scope']>

/** Whether an evaluation can be the source of a swap pass (the server checks the rest). */
export function swappable(evaluation: EvaluationOut): boolean {
  return evaluation.status === 'succeeded' && !evaluation.swap_of && evaluation.candidates.length >= 2
}

/**
 * A position-swap pass: re-judge the same answers with the presentation order reversed, on the same
 * judge, rubric version and settings. A paid action that runs only when confirmed, after a preview of
 * its calls and worst-case cost; one idempotency key per confirmation, so a double click pays once.
 */
export function SwapPass({ runId, evaluation, disabled }: { runId: string; evaluation: EvaluationOut; disabled?: boolean }) {
  const queryClient = useQueryClient()
  const [open, setOpen] = useState(false)
  const [scope, setScope] = useState<Scope>('evaluation')
  const key = useRef(newIdempotencyKey())
  const request: SwapRequest = { evaluation_id: evaluation.id, scope }
  const preview = useDebouncedPost<JudgePreview>(open && !disabled ? `/api/runs/${runId}/evaluations/swap/preview` : null, request)
  const start = useMutation({
    mutationFn: (body: SwapRequest) => post<CreatedEvaluations>(`/api/runs/${runId}/evaluations/swap`, body, key.current),
    onSuccess: () => {
      key.current = newIdempotencyKey()
      setOpen(false)
      queryClient.invalidateQueries({ queryKey: keys.evaluations(runId) })
      queryClient.invalidateQueries({ queryKey: keys.run(runId) })
      queryClient.invalidateQueries({ queryKey: ['calibration'] })
      queryClient.invalidateQueries({ queryKey: ['comparison'] })
    },
  })
  if (!open) {
    return (
      <button type="button" className="small" disabled={disabled} onClick={() => setOpen(true)}>
        Swap order and re-judge…
      </button>
    )
  }
  const data = preview.data
  return (
    <div className="card stack-sm" aria-label="Swap pass" role="group">
      <p className="small muted">
        Re-judges the same answers with the order reversed, using this evaluation’s judge, rubric version and settings. Comparing
        the two shows whether the judge’s preference follows the answer or its position; comparisons then average both.
      </p>
      <label className="row small">
        Scope
        <select value={scope} onChange={(e) => setScope(e.target.value as Scope)}>
          <option value="evaluation">This evaluation</option>
          <option value="run">Every prompt and trial of the run judged this way</option>
        </select>
      </label>
      {data && (
        <div className="stack-sm">
          <p className="small" data-testid="swap-preview">
            {data.calls} judge call{data.calls === 1 ? '' : 's'} · worst-case estimated token cost{' '}
            {data.worst_case_total ? formatMoney(data.worst_case_total) : 'not bounded'}
          </p>
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
      <ErrorMessage error={preview.error ?? start.error} />
      <div className="row">
        <button
          type="button"
          className="primary"
          disabled={!data?.ok || start.isPending || disabled}
          onClick={() => start.mutate(request)}
        >
          {start.isPending ? 'Starting…' : 'Run swap pass'}
        </button>
        <button type="button" onClick={() => setOpen(false)}>
          Cancel
        </button>
        <span className="small muted">A paid call per prompt and trial. Never run automatically or retried.</span>
      </div>
    </div>
  )
}
