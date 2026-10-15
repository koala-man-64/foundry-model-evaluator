import { useMutation, useQueryClient } from '@tanstack/react-query'
import { useEffect, useState } from 'react'

import { put } from '../../api/client'
import { keys, useSettings } from '../../api/hooks'
import type { AppSettings, SettingsOut } from '../../api/types'
import { useToast } from '../../components/toast'
import { Badge, ErrorMessage, Field, Spinner } from '../../components/ui'

export function SettingsPanel() {
  const settings = useSettings()
  const queryClient = useQueryClient()
  const { notify } = useToast()
  const [form, setForm] = useState<AppSettings | null>(null)
  useEffect(() => {
    if (settings.data) setForm(settings.data.settings)
  }, [settings.data])
  const save = useMutation({
    mutationFn: (body: AppSettings) => put<SettingsOut>('/api/settings', body),
    onSuccess: () => {
      notify('Settings saved.', 'ok')
      queryClient.invalidateQueries({ queryKey: keys.settings })
    },
  })
  if (!form || !settings.data) return <Spinner />
  const info = settings.data
  return (
    <div className="stack">
      <form
        className="card stack"
        onSubmit={(e) => {
          e.preventDefault()
          save.mutate(form)
        }}
      >
        <h3>Execution</h3>
        <div className="grid-form">
          <Field label="Concurrent calls" hint="Applies to new runs. Default 3.">
            <input
              type="number"
              min={1}
              max={10}
              value={form.concurrency}
              onChange={(e) => setForm({ ...form, concurrency: Number(e.target.value) || 3 })}
            />
          </Field>
          <Field label="Currency" hint="Estimated token costs use this currency (USD by default).">
            <input value={form.currency} maxLength={3} onChange={(e) => setForm({ ...form, currency: e.target.value.toUpperCase() })} />
          </Field>
          <Field label="Judge input limit (tokens)" hint="Used when a judge profile has no context window.">
            <input
              type="number"
              value={form.judge_input_limit_tokens}
              onChange={(e) => setForm({ ...form, judge_input_limit_tokens: Number(e.target.value) || 100000 })}
            />
          </Field>
        </div>
        <details>
          <summary>Azure Retail Prices filters (advanced)</summary>
          <Field label="Product filters (one OData clause per line)">
            <textarea
              rows={3}
              value={(form.product_filters ?? []).join('\n')}
              onChange={(e) => setForm({ ...form, product_filters: e.target.value.split('\n') })}
            />
          </Field>
        </details>
        <ErrorMessage error={save.error} />
        <div>
          <button type="submit" className="primary" disabled={save.isPending}>
            Save settings
          </button>
        </div>
      </form>
      <div className="card stack-sm">
        <h3>About this installation</h3>
        <div className="small">Version {info.version}</div>
        <div className="small">
          Data folder: <span className="mono">{info.data_dir}</span>
        </div>
        <div className="small row">
          Credential store: <span className="mono">{info.keyring_backend}</span>
          {info.keyring_is_test_backend && <Badge tone="warn">test mode: keys are not persisted</Badge>}
        </div>
        {info.keyring_error && <div className="callout error small">{info.keyring_error}</div>}
      </div>
    </div>
  )
}
