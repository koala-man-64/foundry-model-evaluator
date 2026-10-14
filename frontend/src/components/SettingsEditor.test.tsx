import { fireEvent, render, screen } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { useState } from 'react'
import { describe, expect, it, vi } from 'vitest'

import type { Capabilities, GenerationSettings } from '../api/types'
import { cleanSettings, SettingsEditor, unsupportedSettings } from './SettingsEditor'

const CAPS: Capabilities = {
  max_output_param: 'max_output_tokens',
  structured_output: true,
  system_role: 'developer',
  settings: {
    max_output_tokens: { supported: true, required: true, minimum: 1, maximum: 128000 },
    effort: { supported: true, required: false, allowed: ['low', 'medium', 'high'], provider_default: 'medium' },
    temperature: { supported: false, required: false, note: 'reasoning models ignore sampling settings' },
  },
}

function Harness({ initial, onChange }: { initial: GenerationSettings; onChange: (value: GenerationSettings) => void }) {
  const [value, setValue] = useState(initial)
  return (
    <SettingsEditor
      idPrefix="c1"
      capabilities={CAPS}
      value={value}
      onChange={(next) => {
        setValue(next)
        onChange(next)
      }}
    />
  )
}

describe('SettingsEditor', () => {
  it('disables controls the profile does not support and says why', () => {
    render(<Harness initial={{}} onChange={() => {}} />)
    const temperature = screen.getByLabelText('Temperature')
    expect(temperature).toBeDisabled()
    expect(screen.getByText('Not supported by this model profile — reasoning models ignore sampling settings')).toBeVisible()
    // Settings missing from the descriptor are unsupported too.
    expect(screen.getByLabelText('Top K')).toBeDisabled()
    expect(screen.getByLabelText('Reasoning effort')).toBeEnabled()
    expect(screen.getByText('Default: medium')).toBeVisible()
    expect(screen.getByLabelText('Max output tokens *')).toBeEnabled()
  })

  it('keeps an unsupported value visible and flagged instead of dropping it', async () => {
    const onChange = vi.fn()
    render(<Harness initial={{ temperature: 0.7, effort: 'low' }} onChange={onChange} />)
    const temperature = screen.getByLabelText('Temperature')
    expect(temperature).toBeEnabled()
    expect(temperature).toHaveValue(0.7)
    expect(screen.getByText(/This value is not supported by the selected profile/)).toBeVisible()
    expect(onChange).not.toHaveBeenCalled()

    await userEvent.click(screen.getByRole('button', { name: 'Clear' }))
    expect(onChange).toHaveBeenLastCalledWith({ effort: 'low' })
    expect(screen.getByLabelText('Temperature')).toBeDisabled()
  })

  it('treats an empty choice as the provider default', async () => {
    const onChange = vi.fn()
    render(<Harness initial={{ effort: 'low' }} onChange={onChange} />)
    await userEvent.selectOptions(screen.getByLabelText('Reasoning effort'), 'high')
    expect(onChange).toHaveBeenLastCalledWith({ effort: 'high' })
    await userEvent.selectOptions(screen.getByLabelText('Reasoning effort'), '')
    expect(onChange).toHaveBeenLastCalledWith({})
  })

  it('parses numbers and lists', () => {
    const onChange = vi.fn()
    const caps: Capabilities = {
      ...CAPS,
      settings: { ...CAPS.settings, stop: { supported: true, required: false, max_items: 4 } },
    }
    render(<SettingsEditor idPrefix="c2" capabilities={caps} value={{}} onChange={onChange} />)
    fireEvent.change(screen.getByLabelText('Max output tokens *'), { target: { value: '512' } })
    expect(onChange).toHaveBeenLastCalledWith({ max_output_tokens: 512 })
    fireEvent.change(screen.getByLabelText('Stop sequences'), { target: { value: 'END, , STOP ' } })
    expect(onChange).toHaveBeenLastCalledWith({ stop: ['END', 'STOP'] })
  })

  it('shows validation issues from the server next to the setting', () => {
    render(
      <SettingsEditor
        idPrefix="c3"
        capabilities={CAPS}
        value={{ max_output_tokens: 999999 }}
        onChange={() => {}}
        issues={[{ setting: 'max_output_tokens', message: 'must be at most 128000' }]}
      />,
    )
    expect(screen.getByText('must be at most 128000')).toBeVisible()
  })
})

describe('settings helpers', () => {
  it('lists set-but-unsupported settings by label', () => {
    expect(unsupportedSettings(CAPS, { temperature: 0.2, effort: 'low', top_k: 5 })).toEqual(['Temperature', 'Top K'])
    expect(unsupportedSettings(CAPS, { effort: 'low' })).toEqual([])
    expect(unsupportedSettings(undefined, { seed: 1 })).toEqual(['Seed'])
  })

  it('removes empty values so they mean "provider default"', () => {
    const settings = { effort: '', stop: [], seed: 0, temperature: undefined } as unknown as GenerationSettings
    expect(cleanSettings(settings)).toEqual({ seed: 0 })
  })
})
