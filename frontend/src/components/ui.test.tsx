import { render, screen } from '@testing-library/react'
import { describe, expect, it } from 'vitest'

import { Field } from './ui'

describe('Field', () => {
  it('names the control by its label and describes it with the hint and error', () => {
    render(
      <Field label="Budget" hint="Optional, in USD" error="Enter a decimal amount">
        <input />
      </Field>,
    )
    const input = screen.getByRole('textbox', { name: 'Budget' })
    expect(input).toHaveAccessibleDescription('Optional, in USD Enter a decimal amount')
    expect(input).toHaveAttribute('aria-invalid', 'true')
  })

  it('keeps an id the control already has', () => {
    render(
      <Field label="Region">
        <select id="region">
          <option>eastus2</option>
        </select>
      </Field>,
    )
    expect(screen.getByRole('combobox', { name: 'Region' })).toHaveAttribute('id', 'region')
  })
})
