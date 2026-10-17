import { useId } from 'react'

import type { CheckIn } from '../api/types'

export const CHECK_KINDS: { value: CheckDraft['kind']; label: string; hint: string }[] = [
  { value: 'contains', label: 'Contains', hint: 'The answer contains this text.' },
  { value: 'not_contains', label: 'Does not contain', hint: 'The answer does not contain this text.' },
  { value: 'exact', label: 'Equals', hint: 'The whole answer equals this text, ignoring extra spaces.' },
  {
    value: 'regex',
    label: 'Matches pattern',
    hint: 'A regular expression is found in the answer. Searches have a time limit; an answer it runs out of time on shows "could not check".',
  },
  { value: 'number', label: 'Number', hint: 'A number in the answer is within the tolerance of the value.' },
  { value: 'json_schema', label: 'JSON schema', hint: 'The answer is JSON that matches the schema.' },
]

export const MAX_CHECKS = 10

/** Editable form of a check; text fields stay strings until the suite is saved. */
export interface CheckDraft {
  kind: 'contains' | 'not_contains' | 'exact' | 'regex' | 'number' | 'json_schema'
  text: string // value, pattern, number or JSON schema, depending on the kind
  caseSensitive: boolean
  tolerance: string
  which: 'last' | 'any'
}

export function emptyCheck(): CheckDraft {
  return { kind: 'contains', text: '', caseSensitive: false, tolerance: '0', which: 'last' }
}

export function checkToDraft(check: unknown): CheckDraft {
  const c = (check ?? {}) as Record<string, unknown>
  const kind = (CHECK_KINDS.some((k) => k.value === c.kind) ? c.kind : 'contains') as CheckDraft['kind']
  const draft = emptyCheck()
  draft.kind = kind
  draft.caseSensitive = Boolean(c.case_sensitive)
  if (kind === 'json_schema') draft.text = JSON.stringify(c.schema ?? {}, null, 2)
  else if (kind === 'regex') draft.text = String(c.pattern ?? '')
  else draft.text = String(c.value ?? '')
  if (kind === 'number') {
    draft.tolerance = String(c.tolerance ?? '0')
    draft.which = c.which === 'any' ? 'any' : 'last'
  }
  return draft
}

const NUMBER = /^[-+]?\d+(\.\d+)?([eE][-+]?\d+)?$/

/** The check to save, or a message saying what to fix. Numbers stay strings so they are exact. */
export function draftToCheck(draft: CheckDraft): CheckIn | string {
  const text = draft.text
  switch (draft.kind) {
    case 'contains':
    case 'not_contains':
    case 'exact':
      if (!text) return 'enter the text to look for'
      return { kind: draft.kind, value: text, case_sensitive: draft.caseSensitive }
    case 'regex':
      if (!text) return 'enter a pattern'
      return { kind: 'regex', pattern: text, case_sensitive: draft.caseSensitive }
    case 'number':
      if (!NUMBER.test(text.trim())) return 'enter a number such as 42 or 3.14'
      if (!NUMBER.test(draft.tolerance.trim()) || draft.tolerance.trim().startsWith('-'))
        return 'the tolerance must be zero or a positive number'
      return { kind: 'number', value: text.trim(), tolerance: draft.tolerance.trim(), which: draft.which }
    case 'json_schema': {
      try {
        const schema = JSON.parse(text)
        if (!schema || typeof schema !== 'object' || Array.isArray(schema)) return 'the schema must be a JSON object'
        return { kind: 'json_schema', schema }
      } catch {
        return 'the schema is not valid JSON'
      }
    }
  }
}

/** Free, deterministic checks on the answers to one prompt. */
export function ChecksEditor({
  label,
  checks,
  onChange,
}: {
  label: string
  checks: CheckDraft[]
  onChange: (checks: CheckDraft[]) => void
}) {
  const problemId = useId()
  const update = (index: number, patch: Partial<CheckDraft>) =>
    onChange(checks.map((c, i) => (i === index ? { ...c, ...patch } : c)))
  return (
    <div className="stack-sm" role="group" aria-label={label}>
      {checks.map((check, index) => {
        const problem = draftToCheck(check)
        const kind = CHECK_KINDS.find((k) => k.value === check.kind)
        return (
          <div key={index} className="row" data-testid="check-row">
            <select
              aria-label={`${label} ${index + 1} kind`}
              value={check.kind}
              onChange={(e) => update(index, { kind: e.target.value as CheckDraft['kind'] })}
              title={kind?.hint}
            >
              {CHECK_KINDS.map((k) => (
                <option key={k.value} value={k.value}>
                  {k.label}
                </option>
              ))}
            </select>
            {check.kind === 'json_schema' ? (
              <textarea
                aria-label={`${label} ${index + 1} schema`}
                rows={3}
                className="mono"
                value={check.text}
                placeholder='{"type": "object", "required": ["answer"]}'
                onChange={(e) => update(index, { text: e.target.value })}
              />
            ) : (
              <input
                aria-label={`${label} ${index + 1} value`}
                aria-invalid={typeof problem === 'string' && check.text !== ''}
                aria-describedby={typeof problem === 'string' && check.text !== '' ? `${problemId}-${index}` : undefined}
                value={check.text}
                placeholder={check.kind === 'number' ? '42' : 'Paris'}
                onChange={(e) => update(index, { text: e.target.value })}
              />
            )}
            {check.kind === 'number' && (
              <>
                <label className="row small">
                  ±
                  <input
                    aria-label={`${label} ${index + 1} tolerance`}
                    size={6}
                    value={check.tolerance}
                    onChange={(e) => update(index, { tolerance: e.target.value })}
                  />
                </label>
                <select
                  aria-label={`${label} ${index + 1} which number`}
                  value={check.which}
                  onChange={(e) => update(index, { which: e.target.value as CheckDraft['which'] })}
                >
                  <option value="last">last number</option>
                  <option value="any">any number</option>
                </select>
              </>
            )}
            {['contains', 'not_contains', 'exact', 'regex'].includes(check.kind) && (
              <label className="row small">
                <input
                  type="checkbox"
                  aria-label={`${label} ${index + 1} case-sensitive`}
                  checked={check.caseSensitive}
                  onChange={(e) => update(index, { caseSensitive: e.target.checked })}
                />
                case-sensitive
              </label>
            )}
            <button type="button" className="ghost small" onClick={() => onChange(checks.filter((_, i) => i !== index))}>
              Remove
            </button>
            {typeof problem === 'string' && check.text !== '' && (
              <span id={`${problemId}-${index}`} className="small warn-text" role="status">
                {problem}
              </span>
            )}
          </div>
        )
      })}
      {checks.length < MAX_CHECKS ? (
        <div>
          <button type="button" className="small" onClick={() => onChange([...checks, emptyCheck()])}>
            Add check
          </button>
        </div>
      ) : (
        <p className="small muted">A prompt can have at most {MAX_CHECKS} checks.</p>
      )}
    </div>
  )
}
