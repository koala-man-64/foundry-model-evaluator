import type { Capabilities, GenerationSettings, SettingIssue, SettingSpec } from '../api/types'

export const SETTING_ORDER: { key: keyof GenerationSettings; label: string; kind: 'int' | 'float' | 'enum' | 'list' }[] = [
  { key: 'max_output_tokens', label: 'Max output tokens', kind: 'int' },
  { key: 'effort', label: 'Reasoning effort', kind: 'enum' },
  { key: 'thinking_mode', label: 'Thinking mode', kind: 'enum' },
  { key: 'thinking_budget_tokens', label: 'Thinking budget tokens', kind: 'int' },
  { key: 'thinking_display', label: 'Thinking display', kind: 'enum' },
  { key: 'reasoning_summary', label: 'Reasoning summary', kind: 'enum' },
  { key: 'verbosity', label: 'Verbosity', kind: 'enum' },
  { key: 'temperature', label: 'Temperature', kind: 'float' },
  { key: 'top_p', label: 'Top P', kind: 'float' },
  { key: 'top_k', label: 'Top K', kind: 'int' },
  { key: 'seed', label: 'Seed', kind: 'int' },
  { key: 'stop', label: 'Stop sequences', kind: 'list' },
  { key: 'prompt_cache_ttl', label: 'Prompt cache TTL', kind: 'enum' },
]

export function isSet(value: unknown): boolean {
  if (value === null || value === undefined || value === '') return false
  if (Array.isArray(value)) return value.length > 0
  return true
}

export const EMPTY_SPEC: SettingSpec = { supported: false, required: false }

export function specFor(capabilities: Capabilities | undefined, key: string): SettingSpec {
  return (capabilities?.settings?.[key] as SettingSpec | undefined) ?? EMPTY_SPEC
}

/** Settings that are set but not supported by the profile (they block the run; never dropped). */
export function unsupportedSettings(capabilities: Capabilities | undefined, settings: GenerationSettings): string[] {
  return SETTING_ORDER.filter(({ key }) => isSet(settings[key]) && !specFor(capabilities, key).supported).map(
    ({ label }) => label,
  )
}

/** Remove empty values so "unset" means "provider default". */
export function cleanSettings(settings: GenerationSettings): GenerationSettings {
  const out: Record<string, unknown> = {}
  for (const [key, value] of Object.entries(settings)) if (isSet(value)) out[key] = value
  return out as GenerationSettings
}

interface Props {
  capabilities: Capabilities | undefined
  value: GenerationSettings
  onChange: (value: GenerationSettings) => void
  issues?: SettingIssue[]
  idPrefix: string
}

export function SettingsEditor({ capabilities, value, onChange, issues = [], idPrefix }: Props) {
  const set = (key: keyof GenerationSettings, next: unknown) => {
    const updated: Record<string, unknown> = { ...value }
    if (isSet(next)) updated[key] = next
    else delete updated[key]
    onChange(updated as GenerationSettings)
  }
  return (
    <div className="grid-form settings-grid">
      {SETTING_ORDER.map(({ key, label, kind }) => {
        const spec = specFor(capabilities, key)
        const current = value[key]
        const id = `${idPrefix}-${key}`
        const issue = issues.find((i) => i.setting === key)?.message
        const incompatible = !spec.supported && isSet(current)
        const disabled = !spec.supported && !incompatible
        const hint = !spec.supported
          ? `Not supported by this model profile${spec.note ? ` — ${spec.note}` : ''}`
          : [
              spec.provider_default ? `Default: ${spec.provider_default}` : 'Empty = provider default',
              spec.minimum !== null && spec.minimum !== undefined ? `min ${spec.minimum}` : '',
              spec.maximum !== null && spec.maximum !== undefined ? `max ${spec.maximum}` : '',
              spec.required ? 'required' : '',
            ]
              .filter(Boolean)
              .join(' · ')
        let control
        if (kind === 'enum') {
          control = (
            <select
              id={id}
              value={(current as string | undefined) ?? ''}
              disabled={disabled}
              onChange={(e) => set(key, e.target.value || undefined)}
            >
              <option value="">(provider default)</option>
              {(spec.allowed ?? []).map((option) => (
                <option key={option} value={option}>
                  {option}
                </option>
              ))}
              {incompatible && <option value={String(current)}>{String(current)} (unsupported)</option>}
            </select>
          )
        } else if (kind === 'list') {
          control = (
            <input
              id={id}
              type="text"
              placeholder="comma-separated"
              disabled={disabled}
              value={Array.isArray(current) ? current.join(', ') : ''}
              onChange={(e) =>
                set(
                  key,
                  e.target.value
                    .split(',')
                    .map((s) => s.trim())
                    .filter(Boolean),
                )
              }
            />
          )
        } else {
          control = (
            <input
              id={id}
              type="number"
              inputMode={kind === 'int' ? 'numeric' : 'decimal'}
              step={kind === 'int' ? 1 : 'any'}
              min={spec.minimum ?? undefined}
              max={spec.maximum ?? undefined}
              disabled={disabled}
              value={current === undefined || current === null ? '' : String(current)}
              onChange={(e) => {
                const raw = e.target.value
                if (raw === '') return set(key, undefined)
                const parsed = kind === 'int' ? Number.parseInt(raw, 10) : Number.parseFloat(raw)
                set(key, Number.isNaN(parsed) ? undefined : parsed)
              }}
            />
          )
        }
        return (
          <div key={key} className={`field${issue || incompatible ? ' invalid' : ''}`}>
            <label htmlFor={id} className="label">
              {label}
              {spec.required && spec.supported ? ' *' : ''}
            </label>
            {control}
            <span className="hint">{hint}</span>
            {incompatible && (
              <span className="error-text">
                This value is not supported by the selected profile.{' '}
                <button type="button" className="small" onClick={() => set(key, undefined)}>
                  Clear
                </button>
              </span>
            )}
            {issue && !incompatible && <span className="error-text">{issue}</span>}
          </div>
        )
      })}
    </div>
  )
}
