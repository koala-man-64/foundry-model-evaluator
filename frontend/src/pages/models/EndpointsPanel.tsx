import { useMutation, useQueryClient } from '@tanstack/react-query'
import { useState, type FormEvent } from 'react'

import { del, patch, post, put } from '../../api/client'
import { keys, useEndpoints } from '../../api/hooks'
import type { EndpointIn, EndpointOut, VerifyResult } from '../../api/types'
import { useToast } from '../../components/toast'
import { Badge, Empty, ErrorMessage, Field, Spinner } from '../../components/ui'
import { formatDate } from '../../lib/format'

const EMPTY: EndpointIn = { name: '', base_url: 'https://', region: '', auth_style: 'api-key' }

function EndpointForm({ initial, onDone }: { initial?: EndpointOut; onDone: () => void }) {
  const queryClient = useQueryClient()
  const [form, setForm] = useState<EndpointIn>(
    initial ? { name: initial.name, base_url: initial.base_url, region: initial.region, auth_style: initial.auth_style } : EMPTY,
  )
  const save = useMutation({
    mutationFn: (body: EndpointIn) =>
      initial ? patch<EndpointOut>(`/api/endpoints/${initial.id}`, body) : post<EndpointOut>('/api/endpoints', body),
    onSuccess: () => {
      queryClient.invalidateQueries({ queryKey: keys.endpoints })
      onDone()
    },
  })
  const submit = (event: FormEvent) => {
    event.preventDefault()
    save.mutate(form)
  }
  return (
    <form className="card stack" onSubmit={submit} aria-label={initial ? 'Edit endpoint' : 'New endpoint'}>
      <h3>{initial ? `Edit ${initial.name}` : 'New endpoint'}</h3>
      <div className="grid-form">
        <Field label="Name">
          <input required value={form.name} onChange={(e) => setForm({ ...form, name: e.target.value })} />
        </Field>
        <Field label="Resource URL" hint="e.g. https://my-resource.services.ai.azure.com">
          <input required value={form.base_url} onChange={(e) => setForm({ ...form, base_url: e.target.value })} />
        </Field>
        <Field label="Azure region" hint="Used to look up prices, e.g. eastus2">
          <input value={form.region} onChange={(e) => setForm({ ...form, region: e.target.value.trim().toLowerCase() })} />
        </Field>
        <Field label="Auth header">
          <select
            value={form.auth_style}
            onChange={(e) => setForm({ ...form, auth_style: e.target.value as EndpointIn['auth_style'] })}
          >
            <option value="api-key">api-key (Azure)</option>
            <option value="bearer">Authorization: Bearer</option>
          </select>
        </Field>
      </div>
      {initial && (
        <p className="small muted">Changing the host deletes the stored key, so a key is never sent to a different host.</p>
      )}
      <ErrorMessage error={save.error} />
      <div className="row">
        <button type="submit" className="primary" disabled={save.isPending}>
          Save endpoint
        </button>
        <button type="button" onClick={onDone}>
          Cancel
        </button>
      </div>
    </form>
  )
}

function KeyControls({ endpoint }: { endpoint: EndpointOut }) {
  const queryClient = useQueryClient()
  const { notify } = useToast()
  const [key, setKey] = useState('')
  const [verify, setVerify] = useState<VerifyResult | null>(null)
  const refresh = () => queryClient.invalidateQueries({ queryKey: keys.endpoints })
  const save = useMutation({
    mutationFn: (apiKey: string) => put(`/api/endpoints/${endpoint.id}/key`, { api_key: apiKey }),
    onSuccess: () => {
      setKey('') // the key never stays in the page
      notify('API key saved to Windows Credential Manager.', 'ok')
      refresh()
    },
    onError: () => setKey(''),
  })
  const remove = useMutation({ mutationFn: () => del(`/api/endpoints/${endpoint.id}/key`), onSuccess: refresh })
  const check = useMutation({ mutationFn: () => post<VerifyResult>(`/api/endpoints/${endpoint.id}/verify`), onSuccess: setVerify })
  return (
    <div className="stack-sm">
      <form
        className="row"
        onSubmit={(e) => {
          e.preventDefault()
          if (key.trim()) save.mutate(key)
        }}
      >
        <input
          type="password"
          aria-label={`API key for ${endpoint.name}`}
          autoComplete="off"
          spellCheck={false}
          placeholder={endpoint.key_configured ? 'Replace the stored key' : 'Paste API key'}
          value={key}
          onChange={(e) => setKey(e.target.value)}
        />
        <button type="submit" disabled={!key.trim() || save.isPending}>
          {endpoint.key_configured ? 'Replace key' : 'Save key'}
        </button>
        {endpoint.key_configured && (
          <>
            <button type="button" onClick={() => check.mutate()} disabled={check.isPending}>
              Verify (free)
            </button>
            <button type="button" className="danger" onClick={() => remove.mutate()}>
              Remove key
            </button>
          </>
        )}
      </form>
      {verify && <div className={`callout small ${verify.ok ? 'ok' : 'warn'}`}>{verify.message}</div>}
      <ErrorMessage error={save.error ?? remove.error ?? check.error} />
    </div>
  )
}

export function EndpointsPanel() {
  const endpoints = useEndpoints()
  const queryClient = useQueryClient()
  const [editing, setEditing] = useState<EndpointOut | 'new' | null>(null)
  const remove = useMutation({
    mutationFn: (id: string) => del(`/api/endpoints/${id}`),
    onSuccess: () => queryClient.invalidateQueries({ queryKey: keys.endpoints }),
  })
  if (endpoints.isLoading) return <Spinner />
  return (
    <div className="stack">
      <div className="row-between">
        <p className="muted">
          An endpoint is a Foundry resource. Keys are stored in Windows Credential Manager and are never shown again.
        </p>
        <button type="button" className="primary" onClick={() => setEditing('new')}>
          Add endpoint
        </button>
      </div>
      {editing && <EndpointForm initial={editing === 'new' ? undefined : editing} onDone={() => setEditing(null)} />}
      <ErrorMessage error={endpoints.error ?? remove.error} />
      {endpoints.data?.length === 0 && !editing && <Empty>No endpoints yet.</Empty>}
      {endpoints.data?.map((endpoint) => (
        <div key={endpoint.id} className="card stack-sm" data-testid="endpoint">
          <div className="row-between">
            <div>
              <div className="card-title">{endpoint.name}</div>
              <div className="small muted mono">{endpoint.base_url}</div>
              <div className="small muted">
                Region {endpoint.region || '—'} · {endpoint.auth_style} header
              </div>
            </div>
            <div className="row">
              {endpoint.key_configured ? (
                <Badge tone="ok" title={endpoint.key_set_at ? `Saved ${formatDate(endpoint.key_set_at)}` : undefined}>
                  key stored
                </Badge>
              ) : (
                <Badge tone="warn">no key</Badge>
              )}
              <button type="button" className="small" onClick={() => setEditing(endpoint)}>
                Edit
              </button>
              <button type="button" className="small danger" onClick={() => remove.mutate(endpoint.id)}>
                Delete
              </button>
            </div>
          </div>
          <KeyControls endpoint={endpoint} />
        </div>
      ))}
    </div>
  )
}
