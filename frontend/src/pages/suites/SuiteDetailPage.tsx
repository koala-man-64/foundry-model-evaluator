import { useMutation, useQueryClient } from '@tanstack/react-query'
import { useEffect, useRef, useState } from 'react'
import { Link, useNavigate, useParams } from 'react-router'

import { del, newIdempotencyKey, post } from '../../api/client'
import { useRuns, useSuite } from '../../api/hooks'
import type { CreatedRun, RunPreview, Suite } from '../../api/types'
import { useDebouncedPost } from '../../api/useDebouncedPost'
import { Badge, Empty, ErrorMessage, Field, Spinner, StatusBadge } from '../../components/ui'
import { formatDate } from '../../lib/format'
import { formatMoney, parseDecimal } from '../../lib/money'
import { TotalCell } from '../HistoryPage'
import { ComparisonTable } from './ComparisonTable'
import { SuiteEditor } from './SuiteEditor'

function RunSuitePanel({ suite }: { suite: Suite }) {
  const navigate = useNavigate()
  const queryClient = useQueryClient()
  const [title, setTitle] = useState('')
  const [budget, setBudget] = useState('')
  const budgetValid = budget.trim() === '' || parseDecimal(budget.trim()) !== null
  const body = budgetValid ? { title, budget: budget.trim() || null } : null
  const preview = useDebouncedPost<RunPreview>(`/api/suites/${suite.id}/preview`, body)
  const runKey = useRef(newIdempotencyKey())
  const start = useMutation({
    mutationFn: () => post<CreatedRun>(`/api/suites/${suite.id}/runs`, body, runKey.current),
    onSuccess: (result) => {
      runKey.current = newIdempotencyKey()
      queryClient.invalidateQueries({ queryKey: ['runs'] })
      navigate(`/runs/${result.run_id}`)
    },
  })
  const data = preview.data
  return (
    <section className="card stack" aria-label="Run suite">
      <h2>Run this suite</h2>
      <div className="grid-form">
        <Field label="Run title (optional)">
          <input value={title} placeholder={suite.name} onChange={(e) => setTitle(e.target.value)} />
        </Field>
        <Field
          label="Budget (optional, USD)"
          hint="Calls whose worst-case cost could exceed the budget are skipped (judge calls included)."
          error={budgetValid ? null : 'Enter a decimal amount such as 0.50'}
        >
          <input inputMode="decimal" value={budget} placeholder="e.g. 0.50" onChange={(e) => setBudget(e.target.value)} />
        </Field>
      </div>
      {data && (
        <div className="stack-sm">
          <p className="small">
            {suite.prompts.length} prompt{suite.prompts.length === 1 ? '' : 's'} × {suite.candidates.length} candidate
            {suite.candidates.length === 1 ? '' : 's'} × {suite.trials} trial{suite.trials === 1 ? '' : 's'} ={' '}
            <strong>{data.calls} calls</strong> · worst-case estimated token cost{' '}
            {data.worst_case_total ? formatMoney(data.worst_case_total) : 'not bounded (set max output tokens and rates)'}
            {suite.judge?.enabled ? ' · plus automatic judging after the responses finish' : ''}
          </p>
          {data.errors.length > 0 && (
            <div className="callout error small" role="alert">
              <strong>Fix before running</strong>
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
          disabled={!data?.ok || !preview.current || start.isPending || suite.archived}
          onClick={() => start.mutate()}
        >
          {start.isPending ? 'Starting…' : 'Run suite'}
        </button>
        <span className="small muted">Paid calls are made only when you press Run. Nothing is retried automatically.</span>
      </div>
    </section>
  )
}

export function SuiteDetailPage() {
  const { suiteId = '' } = useParams()
  const navigate = useNavigate()
  const queryClient = useQueryClient()
  const suite = useSuite(suiteId)
  const runs = useRuns(`?suite_id=${encodeURIComponent(suiteId)}`)
  const [editing, setEditing] = useState(false)
  const [selected, setSelected] = useState<string[] | null>(null)
  const archive = useMutation({
    mutationFn: (archived: boolean) => post<Suite>(`/api/suites/${suiteId}/archive`, { archived }),
    onSuccess: () => {
      queryClient.invalidateQueries({ queryKey: ['suite', suiteId] })
      queryClient.invalidateQueries({ queryKey: ['suites'] })
    },
  })
  const remove = useMutation({
    mutationFn: () => del(`/api/suites/${suiteId}`),
    onSuccess: () => {
      queryClient.invalidateQueries({ queryKey: ['suites'] })
      navigate('/suites')
    },
  })
  const finished = (runs.data ?? []).filter((r) => !['queued', 'running'].includes(r.run.status))
  useEffect(() => {
    if (selected === null && finished.length > 0) setSelected([finished[0].run.id])
  }, [finished, selected])

  if (suite.isLoading) return <Spinner />
  if (suite.error) return <ErrorMessage error={suite.error} />
  if (!suite.data) return <Empty>Suite not found.</Empty>
  const data = suite.data
  if (editing) return <SuiteEditor suite={data} onSaved={() => setEditing(false)} onCancel={() => setEditing(false)} />
  const toggle = (id: string) =>
    setSelected((current) => {
      const list = current ?? []
      return list.includes(id) ? list.filter((x) => x !== id) : [...list, id]
    })
  return (
    <div className="stack">
      <div className="page-header">
        <div>
          <h1>
            {data.name} {data.archived && <Badge>archived</Badge>}
          </h1>
          <p>
            {data.prompts.length} prompts · {data.candidates.length} candidates · {data.trials} trial{data.trials === 1 ? '' : 's'}
            {data.judge?.enabled ? ' · automatic judging on' : ''}
          </p>
        </div>
        <div className="row">
          <button type="button" onClick={() => setEditing(true)}>
            Edit suite
          </button>
          <button type="button" onClick={() => archive.mutate(!data.archived)}>
            {data.archived ? 'Restore' : 'Archive'}
          </button>
          <button
            type="button"
            className="danger"
            onClick={() => {
              if (window.confirm('Delete this suite? Its runs stay in History.')) remove.mutate()
            }}
          >
            Delete
          </button>
        </div>
      </div>
      <ErrorMessage error={archive.error ?? remove.error} />
      {data.description && <p>{data.description}</p>}
      <RunSuitePanel suite={data} />

      <section className="stack" aria-label="Suite runs">
        <h2>Runs</h2>
        {runs.isLoading && <Spinner />}
        {runs.data?.length === 0 && <Empty>This suite has not been run yet.</Empty>}
        {runs.data && runs.data.length > 0 && (
          <div className="card table-wrap">
            <table>
              <thead>
                <tr>
                  <th>Compare</th>
                  <th>Created</th>
                  <th>Run</th>
                  <th>Status</th>
                  <th className="num">Calls</th>
                  <th className="num">Combined (estimated token cost)</th>
                </tr>
              </thead>
              <tbody>
                {runs.data.map((summary) => {
                  const total = Object.values(summary.counts).reduce((a, b) => a + b, 0)
                  return (
                    <tr key={summary.run.id}>
                      <td>
                        <input
                          type="checkbox"
                          aria-label={`Compare ${summary.run.title}`}
                          checked={(selected ?? []).includes(summary.run.id)}
                          onChange={() => toggle(summary.run.id)}
                        />
                      </td>
                      <td className="nowrap small">{formatDate(summary.run.created_at)}</td>
                      <td>
                        <Link to={`/runs/${summary.run.id}`}>{summary.run.title || 'Untitled run'}</Link>
                      </td>
                      <td>
                        <StatusBadge status={summary.run.status} />
                      </td>
                      <td className="num">
                        {summary.counts.succeeded ?? 0}/{total}
                      </td>
                      <td className="num">
                        <TotalCell total={summary.combined_cost} />
                      </td>
                    </tr>
                  )
                })}
              </tbody>
            </table>
          </div>
        )}
      </section>

      {finished.length > 0 && (
        <section className="stack" aria-label="Comparison of selected runs">
          <h2>Comparison</h2>
          <ComparisonTable runIds={selected ?? []} />
        </section>
      )}
    </div>
  )
}
