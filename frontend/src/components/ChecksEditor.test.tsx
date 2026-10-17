import { render, screen } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { useState } from 'react'
import { describe, expect, it } from 'vitest'

import { type CheckDraft, ChecksEditor, checkToDraft, draftToCheck, emptyCheck } from './ChecksEditor'

describe('check drafts', () => {
  it('round-trips every kind', () => {
    const checks = [
      { kind: 'contains', value: 'Paris', case_sensitive: true },
      { kind: 'not_contains', value: 'as an AI', case_sensitive: false },
      { kind: 'exact', value: '42', case_sensitive: false },
      { kind: 'regex', pattern: '^[0-9]{4}$', case_sensitive: true },
      { kind: 'number', value: '3.14', tolerance: '0.01', which: 'any' },
      { kind: 'json_schema', schema: { type: 'object', required: ['answer'] } },
    ]
    for (const check of checks) expect(draftToCheck(checkToDraft(check))).toEqual(check)
  })

  it('says what to fix instead of saving a check that cannot run', () => {
    const draft = (patch: Partial<CheckDraft>): CheckDraft => ({ ...emptyCheck(), ...patch })
    expect(draftToCheck(draft({ kind: 'contains', text: '' }))).toBe('enter the text to look for')
    expect(draftToCheck(draft({ kind: 'number', text: 'forty' }))).toMatch(/enter a number/)
    expect(draftToCheck(draft({ kind: 'number', text: '4', tolerance: '-1' }))).toMatch(/tolerance/)
    expect(draftToCheck(draft({ kind: 'json_schema', text: '{nope' }))).toBe('the schema is not valid JSON')
    expect(draftToCheck(draft({ kind: 'json_schema', text: '[1]' }))).toBe('the schema must be a JSON object')
  })
})

function Harness() {
  const [checks, setChecks] = useState<CheckDraft[]>([])
  return (
    <>
      <ChecksEditor label="Prompt 1 check" checks={checks} onChange={setChecks} />
      <output data-testid="state">{JSON.stringify(checks.map(draftToCheck))}</output>
    </>
  )
}

describe('ChecksEditor', () => {
  it('adds, edits and removes checks', async () => {
    render(<Harness />)
    await userEvent.click(screen.getByRole('button', { name: 'Add check' }))
    await userEvent.type(screen.getByLabelText('Prompt 1 check 1 value'), 'Paris')
    await userEvent.click(screen.getByRole('button', { name: 'Add check' }))
    await userEvent.selectOptions(screen.getByLabelText('Prompt 1 check 2 kind'), 'number')
    await userEvent.type(screen.getByLabelText('Prompt 1 check 2 value'), '42')
    expect(JSON.parse(screen.getByTestId('state').textContent ?? '[]')).toEqual([
      { kind: 'contains', value: 'Paris', case_sensitive: false },
      { kind: 'number', value: '42', tolerance: '0', which: 'last' },
    ])
    await userEvent.click(screen.getAllByRole('button', { name: 'Remove' })[0])
    expect(screen.getAllByTestId('check-row')).toHaveLength(1)
  })
})
