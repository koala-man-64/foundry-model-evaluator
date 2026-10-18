import { useMutation, useQueryClient } from '@tanstack/react-query'
import { useState, type FormEvent } from 'react'
import { Link } from 'react-router'

import { post, put } from '../../api/client'
import { useEndpoints, usePresets, useProfiles } from '../../api/hooks'
import type { ApiStyle, Capabilities, EndpointOut, ModelProfile, PresetOut, ProfileFields, SettingSpec } from '../../api/types'
import { EMPTY_SPEC, SETTING_ORDER } from '../../components/SettingsEditor'
import { Badge, Empty, ErrorMessage, Field, Spinner } from '../../components/ui'
import { API_STYLE_LABELS } from '../../lib/format'

const ROUTES: Record<ApiStyle, { value: ProfileFields['route']; label: string }[]> = {
  openai_responses: [
    { value: 'openai_v1', label: 'OpenAI v1 (/openai/v1/responses)' },
    { value: 'custom', label: 'Custom path' },
  ],
  openai_chat: [
    { value: 'openai_v1', label: 'OpenAI v1 (/openai/v1/chat/completions)' },
    { value: 'foundry_models', label: 'Foundry Models (/models/chat/completions)' },
    { value: 'custom', label: 'Custom path' },
  ],
  anthropic_messages: [
    { value: 'anthropic', label: 'Claude (/anthropic/v1/messages)' },
    { value: 'custom', label: 'Custom path' },
  ],
  claude_cli: [{ value: 'cli', label: 'Local CLI subprocess (claude -p)' }],
  codex_cli: [{ value: 'cli', label: 'Local CLI subprocess (codex exec)' }],
}

function presetsFor(presets: PresetOut[], style: ApiStyle) {
  return presets.filter((p) => p.api_styles.includes(style))
}

function CapabilityEditor({ value, onChange }: { value: Capabilities; onChange: (value: Capabilities) => void }) {
  const update = (key: string, patchSpec: Partial<SettingSpec>) => {
    const current = (value.settings?.[key] as SettingSpec | undefined) ?? EMPTY_SPEC
    onChange({ ...value, settings: { ...value.settings, [key]: { ...current, ...patchSpec } } })
  }
  return (
    <div className="stack-sm">
      <div className="table-wrap">
        <table className="compact">
          <thead>
            <tr>
              <th>Setting</th>
              <th>Supported</th>
              <th>Allowed values</th>
              <th>Min</th>
              <th>Max</th>
              <th>Required</th>
            </tr>
          </thead>
          <tbody>
            {SETTING_ORDER.map(({ key, label, kind }) => {
              const spec = (value.settings?.[key] as SettingSpec | undefined) ?? EMPTY_SPEC
              return (
                <tr key={key}>
                  <td>
                    {label}
                    {spec.note && <div className="small muted">{spec.note}</div>}
                  </td>
                  <td>
                    <input
                      type="checkbox"
                      aria-label={`${label} supported`}
                      checked={spec.supported}
                      onChange={(e) => update(key, { supported: e.target.checked })}
                    />
                  </td>
                  <td>
                    {kind === 'enum' ? (
                      <input
                        aria-label={`${label} allowed values`}
                        value={(spec.allowed ?? []).join(', ')}
                        onChange={(e) =>
                          update(key, {
                            allowed: e.target.value
                              .split(',')
                              .map((s) => s.trim())
                              .filter(Boolean),
                          })
                        }
                      />
                    ) : kind === 'list' ? (
                      <input
                        aria-label={`${label} max items`}
                        type="number"
                        placeholder="max items"
                        value={spec.max_items ?? ''}
                        onChange={(e) => update(key, { max_items: e.target.value ? Number(e.target.value) : null })}
                      />
                    ) : (
                      <span className="muted">—</span>
                    )}
                  </td>
                  <td>
                    {kind === 'int' || kind === 'float' ? (
                      <input
                        aria-label={`${label} minimum`}
                        type="number"
                        value={spec.minimum ?? ''}
                        onChange={(e) => update(key, { minimum: e.target.value === '' ? null : Number(e.target.value) })}
                      />
                    ) : null}
                  </td>
                  <td>
                    {kind === 'int' || kind === 'float' ? (
                      <input
                        aria-label={`${label} maximum`}
                        type="number"
                        value={spec.maximum ?? ''}
                        onChange={(e) => update(key, { maximum: e.target.value === '' ? null : Number(e.target.value) })}
                      />
                    ) : null}
                  </td>
                  <td>
                    <input
                      type="checkbox"
                      aria-label={`${label} required`}
                      checked={Boolean(spec.required)}
                      onChange={(e) => update(key, { required: e.target.checked })}
                    />
                  </td>
                </tr>
              )
            })}
          </tbody>
        </table>
      </div>
      <div className="grid-form">
        <Field label="Output limit parameter">
          <select
            value={value.max_output_param}
            onChange={(e) => onChange({ ...value, max_output_param: e.target.value as Capabilities['max_output_param'] })}
          >
            <option value="max_output_tokens">max_output_tokens (Responses)</option>
            <option value="max_completion_tokens">max_completion_tokens</option>
            <option value="max_tokens">max_tokens</option>
          </select>
        </Field>
        <Field label="System role (Chat Completions)">
          <select
            value={value.system_role}
            onChange={(e) => onChange({ ...value, system_role: e.target.value as Capabilities['system_role'] })}
          >
            <option value="system">system</option>
            <option value="developer">developer</option>
          </select>
        </Field>
        <label className="checkbox">
          <input
            type="checkbox"
            checked={Boolean(value.structured_output)}
            onChange={(e) => onChange({ ...value, structured_output: e.target.checked })}
          />
          Structured JSON output (used by the judge)
        </label>
      </div>
    </div>
  )
}

function blankFields(endpoint: EndpointOut | undefined, allPresets: PresetOut[]): ProfileFields {
  let defaultStyle: ApiStyle = 'openai_responses'
  if (endpoint?.base_url === 'local://claude') defaultStyle = 'claude_cli'
  else if (endpoint?.base_url === 'local://codex') defaultStyle = 'codex_cli'
  const preset = presetsFor(allPresets, defaultStyle)[0] ?? allPresets[0]
  const style = (preset?.api_styles.includes(defaultStyle) ? defaultStyle : (preset?.api_styles[0] ?? 'openai_responses')) as ApiStyle
  return {
    endpoint_id: endpoint?.id ?? '',
    name: '',
    api_style: style,
    route: ROUTES[style][0].value,
    custom_path: null,
    api_version: null,
    deployment: '',
    publisher: '',
    model_name: '',
    model_version: '',
    context_window: null,
    max_output_tokens: null,
    preset: preset?.id ?? 'openai-reasoning',
    capabilities: (preset?.capabilities[style] as Capabilities) ?? { settings: {} },
    deployment_type: 'global',
    processing_tier: 'standard',
    pricing_region: null,
    meter_terms: '',
    long_context_threshold: null,
    streaming: style !== 'claude_cli',
    include_usage: true,
    timeout_connect_s: 15,
    timeout_read_s: 600,
    timeout_total_s: 900,
    notes: '',
  }
}

function toFields(profile: ModelProfile): ProfileFields {
  const { id: _id, archived: _a, created_at: _c, updated_at: _u, ...fields } = profile
  return fields as ProfileFields
}

function numberOrNull(value: string): number | null {
  if (value.trim() === '') return null
  const parsed = Number(value)
  return Number.isFinite(parsed) ? parsed : null
}

export function ProfileForm({ initial, onDone }: { initial?: ModelProfile; onDone: () => void }) {
  const endpoints = useEndpoints()
  const presets = usePresets()
  if (presets.isLoading || endpoints.isLoading) return <Spinner />
  if (presets.error || endpoints.error) return <ErrorMessage error={presets.error ?? endpoints.error} />
  if (!endpoints.data?.length) return <Empty>Add an endpoint first.</Empty>
  // The form state is initialised only once presets and endpoints are loaded.
  return (
    <ProfileFormBody initial={initial} onDone={onDone} endpoints={endpoints.data} allPresets={presets.data?.presets ?? []} />
  )
}

function ProfileFormBody({
  initial,
  onDone,
  endpoints,
  allPresets,
}: {
  initial?: ModelProfile
  onDone: () => void
  endpoints: EndpointOut[]
  allPresets: PresetOut[]
}) {
  const queryClient = useQueryClient()
  const [form, setForm] = useState<ProfileFields>(() =>
    initial ? toFields(initial) : blankFields(endpoints[0], allPresets),
  )
  const save = useMutation({
    mutationFn: (body: ProfileFields) =>
      initial ? put<ModelProfile>(`/api/profiles/${initial.id}`, body) : post<ModelProfile>('/api/profiles', body),
    onSuccess: () => {
      queryClient.invalidateQueries({ queryKey: ['profiles'] })
      queryClient.invalidateQueries({ queryKey: ['rates'] })
      onDone()
    },
  })
  const applyPreset = (presetId: string, style: ApiStyle) => {
    const preset = allPresets.find((p) => p.id === presetId)
    const caps = preset?.capabilities[style] as Capabilities | undefined
    setForm((f) => ({ ...f, preset: presetId, api_style: style, capabilities: caps ?? f.capabilities }))
  }
  const setStyle = (style: ApiStyle) => {
    const preset = presetsFor(allPresets, style).find((p) => p.id === form.preset) ?? presetsFor(allPresets, style)[0]
    setForm((f) => ({ ...f, api_style: style, route: ROUTES[style][0].value }))
    if (preset) applyPreset(preset.id, style)
  }
  const handleEndpointChange = (endpointId: string) => {
    const ep = endpoints.find((e) => e.id === endpointId)
    setForm((f) => ({ ...f, endpoint_id: endpointId }))
    if (!initial && ep) {
      if (ep.base_url === 'local://claude' && form.api_style !== 'claude_cli') setStyle('claude_cli')
      else if (ep.base_url === 'local://codex' && form.api_style !== 'codex_cli') setStyle('codex_cli')
    }
  }
  const submit = (event: FormEvent) => {
    event.preventDefault()
    save.mutate(form)
  }
  const presetChoices = presetsFor(allPresets, form.api_style)
  const preset = allPresets.find((p) => p.id === form.preset)
  const deploymentHint =
    form.api_style === 'claude_cli'
      ? 'Passed as --model (e.g. sonnet, opus, haiku, claude-sonnet-4-6)'
      : form.api_style === 'codex_cli'
        ? 'Passed as -m (e.g. gpt-5.4, gpt-5.4-mini, o3)'
        : 'Sent as the model field'
  return (
    <form className="card stack" onSubmit={submit} aria-label={initial ? 'Edit model profile' : 'New model profile'}>
      <h3>{initial ? `Edit ${initial.name}` : 'New model profile'}</h3>
      <div className="grid-form">
        <Field label="Display name">
          <input required value={form.name} onChange={(e) => setForm({ ...form, name: e.target.value })} />
        </Field>
        <Field label="Endpoint">
          <select value={form.endpoint_id} onChange={(e) => handleEndpointChange(e.target.value)}>
            {endpoints.map((e) => (
              <option key={e.id} value={e.id}>
                {e.name}
              </option>
            ))}
          </select>
        </Field>
        <Field label="API">
          <select value={form.api_style} onChange={(e) => setStyle(e.target.value as ApiStyle)}>
            <optgroup label="Microsoft Foundry / HTTP">
              {(['openai_responses', 'openai_chat', 'anthropic_messages'] as ApiStyle[]).map((style) => (
                <option key={style} value={style}>
                  {API_STYLE_LABELS[style]}
                </option>
              ))}
            </optgroup>
            <optgroup label="Local CLI">
              {(['claude_cli', 'codex_cli'] as ApiStyle[]).map((style) => (
                <option key={style} value={style}>
                  {API_STYLE_LABELS[style]}
                </option>
              ))}
            </optgroup>
          </select>
        </Field>
        <Field label="Route">
          <select value={form.route} onChange={(e) => setForm({ ...form, route: e.target.value as ProfileFields['route'] })}>
            {ROUTES[form.api_style].map((route) => (
              <option key={route.value} value={route.value}>
                {route.label}
              </option>
            ))}
          </select>
        </Field>
        {form.route === 'custom' && (
          <Field label="Custom path" hint="Appended to the resource URL, e.g. /openai/deployments/x/chat/completions">
            <input value={form.custom_path ?? ''} onChange={(e) => setForm({ ...form, custom_path: e.target.value || null })} />
          </Field>
        )}
        {(form.route === 'foundry_models' || form.route === 'custom') && (
          <Field label="api-version" hint={form.route === 'foundry_models' ? 'Default 2024-05-01-preview' : 'Optional'}>
            <input value={form.api_version ?? ''} onChange={(e) => setForm({ ...form, api_version: e.target.value || null })} />
          </Field>
        )}
        <Field label="Deployment name" hint={deploymentHint}>
          <input required value={form.deployment} onChange={(e) => setForm({ ...form, deployment: e.target.value })} />
        </Field>
      </div>

      <h4>Model metadata</h4>
      <div className="grid-form">
        <Field label="Publisher">
          <input value={form.publisher} onChange={(e) => setForm({ ...form, publisher: e.target.value })} />
        </Field>
        <Field label="Model name">
          <input value={form.model_name} onChange={(e) => setForm({ ...form, model_name: e.target.value })} />
        </Field>
        <Field label="Model version">
          <input value={form.model_version} onChange={(e) => setForm({ ...form, model_version: e.target.value })} />
        </Field>
        <Field label="Context window (tokens)" hint="Needed to use this profile as a judge">
          <input
            type="number"
            value={form.context_window ?? ''}
            onChange={(e) => setForm({ ...form, context_window: numberOrNull(e.target.value) })}
          />
        </Field>
        <Field label="Max output tokens">
          <input
            type="number"
            value={form.max_output_tokens ?? ''}
            onChange={(e) => setForm({ ...form, max_output_tokens: numberOrNull(e.target.value) })}
          />
        </Field>
      </div>

      <h4>Capabilities</h4>
      <Field label="Preset" hint={preset?.description}>
        <select value={form.preset} onChange={(e) => applyPreset(e.target.value, form.api_style)}>
          {presetChoices.map((p) => (
            <option key={p.id} value={p.id}>
              {p.label}
            </option>
          ))}
        </select>
      </Field>
      <p className="small muted">
        Presets are starting points; model capabilities change. Unsupported controls are disabled on the Compare page and
        requests that set them are rejected.
      </p>
      <details>
        <summary>Edit capabilities</summary>
        <CapabilityEditor value={form.capabilities} onChange={(capabilities) => setForm({ ...form, capabilities })} />
      </details>

      <h4>Pricing identity</h4>
      <div className="grid-form">
        <Field label="Deployment type">
          <select
            value={form.deployment_type}
            onChange={(e) => setForm({ ...form, deployment_type: e.target.value as ProfileFields['deployment_type'] })}
          >
            <option value="global">Global</option>
            <option value="data_zone">Data Zone</option>
            <option value="regional">Regional</option>
          </select>
        </Field>
        <Field label="Processing tier">
          <select
            value={form.processing_tier}
            onChange={(e) => setForm({ ...form, processing_tier: e.target.value as ProfileFields['processing_tier'] })}
          >
            <option value="standard">Standard</option>
            <option value="priority">Priority</option>
          </select>
        </Field>
        <Field label="Pricing region override" hint="Defaults to the endpoint region">
          <input
            value={form.pricing_region ?? ''}
            onChange={(e) => setForm({ ...form, pricing_region: e.target.value.trim().toLowerCase() || null })}
          />
        </Field>
        <Field label="Meter terms" hint="Model words in Azure meter names, e.g. '5.6 terra' or 'gpt-4o 1120'">
          <input value={form.meter_terms} onChange={(e) => setForm({ ...form, meter_terms: e.target.value })} />
        </Field>
        <Field label="Long-context threshold (input tokens)" hint="Set when long-context rates apply above a size">
          <input
            type="number"
            value={form.long_context_threshold ?? ''}
            onChange={(e) => setForm({ ...form, long_context_threshold: numberOrNull(e.target.value) })}
          />
        </Field>
      </div>

      <details>
        <summary>Transport</summary>
        <div className="grid-form">
          <label className="checkbox">
            <input type="checkbox" checked={form.streaming} onChange={(e) => setForm({ ...form, streaming: e.target.checked })} />
            Stream responses (measures time to first token)
          </label>
          <label className="checkbox">
            <input
              type="checkbox"
              checked={form.include_usage}
              onChange={(e) => setForm({ ...form, include_usage: e.target.checked })}
            />
            Chat streaming reports usage (stream_options.include_usage)
          </label>
          <Field label="Connect timeout (s)">
            <input
              type="number"
              value={form.timeout_connect_s}
              onChange={(e) => setForm({ ...form, timeout_connect_s: Number(e.target.value) || 15 })}
            />
          </Field>
          <Field label="Read timeout (s)">
            <input
              type="number"
              value={form.timeout_read_s}
              onChange={(e) => setForm({ ...form, timeout_read_s: Number(e.target.value) || 600 })}
            />
          </Field>
          <Field label="Total timeout (s)">
            <input
              type="number"
              value={form.timeout_total_s}
              onChange={(e) => setForm({ ...form, timeout_total_s: Number(e.target.value) || 900 })}
            />
          </Field>
        </div>
      </details>
      <Field label="Notes">
        <textarea rows={2} value={form.notes} onChange={(e) => setForm({ ...form, notes: e.target.value })} />
      </Field>
      <ErrorMessage error={save.error} />
      <div className="row">
        <button type="submit" className="primary" disabled={save.isPending}>
          Save profile
        </button>
        <button type="button" onClick={onDone}>
          Cancel
        </button>
      </div>
    </form>
  )
}

export function ProfilesPanel() {
  const [showArchived, setShowArchived] = useState(false)
  const profiles = useProfiles(showArchived)
  const endpoints = useEndpoints()
  const queryClient = useQueryClient()
  const [editing, setEditing] = useState<ModelProfile | 'new' | null>(null)
  const archive = useMutation({
    mutationFn: ({ id, archived }: { id: string; archived: boolean }) => post(`/api/profiles/${id}/archive`, { archived }),
    onSuccess: () => queryClient.invalidateQueries({ queryKey: ['profiles'] }),
  })
  const endpointName = (id: string) => endpoints.data?.find((e) => e.id === id)?.name ?? '—'
  return (
    <div className="stack">
      <div className="row-between">
        <label className="checkbox">
          <input type="checkbox" checked={showArchived} onChange={(e) => setShowArchived(e.target.checked)} />
          Show archived
        </label>
        <button type="button" className="primary" onClick={() => setEditing('new')} disabled={!endpoints.data?.length}>
          Add model profile
        </button>
      </div>
      {editing && <ProfileForm initial={editing === 'new' ? undefined : editing} onDone={() => setEditing(null)} />}
      {profiles.isLoading && <Spinner />}
      <ErrorMessage error={profiles.error ?? archive.error} />
      {profiles.data?.length === 0 && !editing && <Empty>No model profiles yet.</Empty>}
      {profiles.data && profiles.data.length > 0 && (
        <div className="card table-wrap">
          <table>
            <thead>
              <tr>
                <th>Profile</th>
                <th>Endpoint</th>
                <th>API</th>
                <th>Deployment</th>
                <th>Preset</th>
                <th>Pricing</th>
                <th />
              </tr>
            </thead>
            <tbody>
              {profiles.data.map((profile) => (
                <tr key={profile.id}>
                  <td>
                    {profile.name} {profile.archived && <Badge>archived</Badge>}
                    <div className="small muted">
                      {[profile.publisher, profile.model_name, profile.model_version].filter(Boolean).join(' · ')}
                    </div>
                  </td>
                  <td>{endpointName(profile.endpoint_id)}</td>
                  <td>{API_STYLE_LABELS[profile.api_style] ?? profile.api_style}</td>
                  <td className="mono small">{profile.deployment}</td>
                  <td className="small">{profile.preset}</td>
                  <td>
                    <Link to={`/models/pricing/${profile.id}`}>Rates</Link>
                  </td>
                  <td className="nowrap">
                    <button type="button" className="small" onClick={() => setEditing(profile)}>
                      Edit
                    </button>{' '}
                    <button
                      type="button"
                      className="small"
                      onClick={() => archive.mutate({ id: profile.id, archived: !profile.archived })}
                    >
                      {profile.archived ? 'Restore' : 'Archive'}
                    </button>
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      )}
    </div>
  )
}
