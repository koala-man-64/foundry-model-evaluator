import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import { useState } from 'react'

import { get, patch, post } from '../../api/client'
import { keys, useRubrics } from '../../api/hooks'
import type { Criterion, Rubric, RubricDefinition, RubricVersion } from '../../api/types'
import { Badge, Empty, ErrorMessage, Field, Spinner } from '../../components/ui'
import { formatDate } from '../../lib/format'

const KEY_PATTERN = /^[a-z][a-z0-9_]{0,39}$/

interface CriterionDraft {
  key: string
  name: string
  description: string
  weight: string
}

export function rubricProblems(min: number, max: number, criteria: CriterionDraft[]): string[] {
  const problems: string[] = []
  if (!Number.isInteger(min) || !Number.isInteger(max)) problems.push('The score scale uses whole numbers.')
  else if (min >= max) problems.push('The scale minimum must be lower than the maximum.')
  if (criteria.length === 0) problems.push('Add at least one criterion.')
  const keysSeen = new Set<string>()
  criteria.forEach((c, i) => {
    if (!KEY_PATTERN.test(c.key)) problems.push(`Criterion ${i + 1}: keys use lowercase letters, digits and underscores.`)
    if (keysSeen.has(c.key)) problems.push(`Criterion ${i + 1}: the key "${c.key}" is used twice.`)
    keysSeen.add(c.key)
    if (!c.name.trim()) problems.push(`Criterion ${i + 1}: give it a name.`)
    const weight = Number(c.weight)
    if (!Number.isFinite(weight) || weight <= 0) problems.push(`Criterion ${i + 1}: the weight must be greater than zero.`)
  })
  return problems
}

function toDraft(criteria: Criterion[]): CriterionDraft[] {
  return criteria.map((c) => ({ key: c.key, name: c.name, description: c.description ?? '', weight: String(c.weight ?? 1) }))
}

function RubricEditor({
  initial,
  title,
  withName,
  onSubmit,
  onCancel,
  pending,
  error,
}: {
  initial: RubricDefinition
  title: string
  withName: boolean
  onSubmit: (name: string, definition: RubricDefinition) => void
  onCancel: () => void
  pending: boolean
  error: unknown
}) {
  const [name, setName] = useState('')
  const [min, setMin] = useState(String(initial.scale_min ?? 1))
  const [max, setMax] = useState(String(initial.scale_max ?? 5))
  const [instructions, setInstructions] = useState(initial.instructions ?? '')
  const [criteria, setCriteria] = useState<CriterionDraft[]>(toDraft(initial.criteria))
  const problems = rubricProblems(Number(min), Number(max), criteria)
  if (withName && !name.trim()) problems.unshift('Give the rubric a name.')
  const update = (index: number, change: Partial<CriterionDraft>) =>
    setCriteria((list) => list.map((c, i) => (i === index ? { ...c, ...change } : c)))
  const totalWeight = criteria.reduce((sum, c) => sum + (Number(c.weight) > 0 ? Number(c.weight) : 0), 0)
  return (
    <form
      className="card stack"
      aria-label={title}
      onSubmit={(e) => {
        e.preventDefault()
        if (problems.length) return
        onSubmit(name.trim(), {
          scale_min: Number(min),
          scale_max: Number(max),
          instructions,
          criteria: criteria.map((c) => ({ key: c.key, name: c.name.trim(), description: c.description, weight: Number(c.weight) })),
        })
      }}
    >
      <h3>{title}</h3>
      {withName && (
        <Field label="Rubric name">
          <input value={name} onChange={(e) => setName(e.target.value)} />
        </Field>
      )}
      <div className="grid-form">
        <Field label="Lowest score">
          <input type="number" step={1} value={min} onChange={(e) => setMin(e.target.value)} />
        </Field>
        <Field label="Highest score">
          <input type="number" step={1} value={max} onChange={(e) => setMax(e.target.value)} />
        </Field>
      </div>
      <div className="table-wrap">
        <table className="compact">
          <thead>
            <tr>
              <th>Key</th>
              <th>Name</th>
              <th>Description</th>
              <th>Weight</th>
              <th />
            </tr>
          </thead>
          <tbody>
            {criteria.map((c, index) => (
              <tr key={index}>
                <td>
                  <input aria-label={`Criterion ${index + 1} key`} value={c.key} onChange={(e) => update(index, { key: e.target.value })} />
                </td>
                <td>
                  <input aria-label={`Criterion ${index + 1} name`} value={c.name} onChange={(e) => update(index, { name: e.target.value })} />
                </td>
                <td>
                  <input
                    aria-label={`Criterion ${index + 1} description`}
                    value={c.description}
                    onChange={(e) => update(index, { description: e.target.value })}
                  />
                </td>
                <td>
                  <input
                    aria-label={`Criterion ${index + 1} weight`}
                    inputMode="decimal"
                    value={c.weight}
                    onChange={(e) => update(index, { weight: e.target.value })}
                  />
                  {totalWeight > 0 && Number(c.weight) > 0 && (
                    <div className="small muted">{Math.round((Number(c.weight) / totalWeight) * 100)}%</div>
                  )}
                </td>
                <td>
                  <button
                    type="button"
                    className="small danger"
                    onClick={() => setCriteria((list) => list.filter((_, i) => i !== index))}
                  >
                    Remove
                  </button>
                </td>
              </tr>
            ))}
          </tbody>
        </table>
      </div>
      <div>
        <button
          type="button"
          className="small"
          onClick={() => setCriteria((list) => [...list, { key: '', name: '', description: '', weight: '1' }])}
        >
          Add criterion
        </button>
      </div>
      <Field label="Additional judge instructions (optional)">
        <textarea rows={2} value={instructions} onChange={(e) => setInstructions(e.target.value)} />
      </Field>
      <p className="small muted">Weights are normalized to sum to 100%. Saving creates a new version; earlier versions never change.</p>
      {problems.length > 0 && (
        <div className="callout warn small" role="alert">
          <ul>
            {problems.map((p) => (
              <li key={p}>{p}</li>
            ))}
          </ul>
        </div>
      )}
      <ErrorMessage error={error} />
      <div className="row">
        <button type="submit" className="primary" disabled={pending || problems.length > 0}>
          Save
        </button>
        <button type="button" onClick={onCancel}>
          Cancel
        </button>
      </div>
    </form>
  )
}

function RubricDetail({ rubric }: { rubric: Rubric }) {
  const queryClient = useQueryClient()
  const [editing, setEditing] = useState(false)
  const versions = useQuery({
    queryKey: ['rubric-versions', rubric.id],
    queryFn: () => get<RubricVersion[]>(`/api/rubrics/${rubric.id}/versions`),
  })
  const addVersion = useMutation({
    mutationFn: (definition: RubricDefinition) => post<RubricVersion>(`/api/rubrics/${rubric.id}/versions`, definition),
    onSuccess: () => {
      queryClient.invalidateQueries({ queryKey: keys.rubrics })
      queryClient.invalidateQueries({ queryKey: ['rubric-versions', rubric.id] })
      setEditing(false)
    },
  })
  const archive = useMutation({
    mutationFn: () => patch<Rubric>(`/api/rubrics/${rubric.id}`, { archived: true }),
    onSuccess: () => queryClient.invalidateQueries({ queryKey: keys.rubrics }),
  })
  const latest = rubric.latest.definition
  return (
    <div className="card stack-sm" data-testid="rubric">
      <div className="row-between">
        <div>
          <div className="card-title">
            {rubric.name} {rubric.is_default && <Badge tone="info">default</Badge>}
          </div>
          <div className="small muted">
            Version {rubric.latest.version} of {rubric.version_count} · scores {latest.scale_min}–{latest.scale_max} · updated{' '}
            {formatDate(rubric.updated_at)}
          </div>
        </div>
        <div className="row">
          <button type="button" className="small" onClick={() => setEditing(true)}>
            New version
          </button>
          {!rubric.is_default && (
            <button type="button" className="small" onClick={() => archive.mutate()}>
              Archive
            </button>
          )}
        </div>
      </div>
      <ul className="small">
        {latest.criteria.map((c) => (
          <li key={c.key}>
            <strong>{c.name}</strong> <span className="mono muted">{c.key}</span> · weight {c.weight}
            {c.description ? ` — ${c.description}` : ''}
          </li>
        ))}
      </ul>
      {latest.instructions && <p className="small">Instructions: {latest.instructions}</p>}
      {editing && (
        <RubricEditor
          title={`New version of ${rubric.name}`}
          initial={latest}
          withName={false}
          onSubmit={(_name, definition) => addVersion.mutate(definition)}
          onCancel={() => setEditing(false)}
          pending={addVersion.isPending}
          error={addVersion.error}
        />
      )}
      {(versions.data ?? []).length > 1 && (
        <details>
          <summary>Earlier versions</summary>
          <ul className="small">
            {(versions.data ?? []).slice(1).map((v) => (
              <li key={v.id}>
                v{v.version} ({formatDate(v.created_at)}): {v.definition.criteria.map((c) => c.name).join(', ')}
              </li>
            ))}
          </ul>
        </details>
      )}
      <ErrorMessage error={archive.error} />
    </div>
  )
}

export function RubricsPanel() {
  const queryClient = useQueryClient()
  const rubrics = useRubrics()
  const [creating, setCreating] = useState(false)
  const create = useMutation({
    mutationFn: ({ name, definition }: { name: string; definition: RubricDefinition }) =>
      post<Rubric>('/api/rubrics', { name, definition }),
    onSuccess: () => {
      queryClient.invalidateQueries({ queryKey: keys.rubrics })
      setCreating(false)
    },
  })
  if (rubrics.isLoading) return <Spinner />
  const defaultRubric = rubrics.data?.find((r) => r.is_default)
  return (
    <div className="stack">
      <div className="row-between">
        <p className="muted">Rubrics define the criteria, weights and score scale the judge must use.</p>
        <button type="button" className="primary" onClick={() => setCreating(true)}>
          New rubric
        </button>
      </div>
      {creating && defaultRubric && (
        <RubricEditor
          title="New rubric"
          initial={defaultRubric.latest.definition}
          withName
          onSubmit={(name, definition) => create.mutate({ name, definition })}
          onCancel={() => setCreating(false)}
          pending={create.isPending}
          error={create.error}
        />
      )}
      <ErrorMessage error={rubrics.error} />
      {rubrics.data?.length === 0 && <Empty>No rubrics.</Empty>}
      {rubrics.data?.map((rubric) => (
        <RubricDetail key={rubric.id} rubric={rubric} />
      ))}
    </div>
  )
}
