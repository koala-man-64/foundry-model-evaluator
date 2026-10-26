import { useMutation, useQueryClient } from '@tanstack/react-query'
import { useState, type KeyboardEvent } from 'react'

import { post } from '../api/client'
import type { ModelProfile, ProfileFields } from '../api/types'
import { isCli, modelArgument, sameModel, suggestedVariantName, variantFields, type VariantInput } from '../lib/profiles'
import { ErrorMessage, Field } from './ui'

/**
 * The model a candidate's profile sends, with a way to save a variant of the profile for another model or
 * version. A variant is its own profile, so it gets its own rates and comparisons tell it apart.
 */
export function CandidateModel({
  profile,
  candidateLabel,
  onVariant,
}: {
  profile: ModelProfile | undefined
  candidateLabel: string
  onVariant: (profile: ModelProfile) => void
}) {
  const [open, setOpen] = useState(false)
  if (!profile) return null
  const recordedVersion = !isCli(profile.api_style) && profile.model_version
  return (
    <div className="stack-sm">
      <div className="row small">
        <span className="muted">
          Sends <code>{modelArgument(profile)}</code>
          {recordedVersion && <> · version {profile.model_version}</>}
        </span>
        {!open && (
          <button type="button" className="small" onClick={() => setOpen(true)}>
            New variant…
          </button>
        )}
      </div>
      {open && (
        <VariantForm
          // A fresh form for each source profile.
          key={profile.id}
          source={profile}
          candidateLabel={candidateLabel}
          onCancel={() => setOpen(false)}
          onCreated={(created) => {
            setOpen(false)
            onVariant(created)
          }}
        />
      )}
    </div>
  )
}

function VariantForm({
  source,
  candidateLabel,
  onCancel,
  onCreated,
}: {
  source: ModelProfile
  candidateLabel: string
  onCancel: () => void
  onCreated: (profile: ModelProfile) => void
}) {
  const queryClient = useQueryClient()
  const cli = isCli(source.api_style)
  const [input, setInput] = useState<VariantInput>({
    deployment: source.deployment,
    modelVersion: source.model_version,
    modelName: source.model_name,
    meterTerms: '',
    name: '',
  })
  const [problem, setProblem] = useState<string | null>(null)
  const create = useMutation({
    mutationFn: (body: ProfileFields) => post<ModelProfile>('/api/profiles', body),
    onSuccess: async (created) => {
      // The candidate switches to the variant only once the profile list has it, so its select never shows a gap.
      await queryClient.invalidateQueries({ queryKey: ['profiles'] })
      void queryClient.invalidateQueries({ queryKey: ['rates'] })
      onCreated(created)
    },
  })
  const fields = variantFields(source, input)
  const save = () => {
    if (!input.deployment.trim()) {
      setProblem(cli ? 'Enter the model.' : 'Enter the deployment name.')
    } else if (sameModel(source, input)) {
      setProblem(`Change the model or the version: this is ${source.name}.`)
    } else {
      setProblem(null)
      create.mutate(fields)
    }
  }
  // The suite editor is a form: Enter here saves the variant instead of submitting the suite.
  const onKeyDown = (event: KeyboardEvent) => {
    if (event.key === 'Enter' && event.target instanceof HTMLInputElement) {
      event.preventDefault()
      save()
    }
  }
  const set = (patch: Partial<VariantInput>) => setInput((current) => ({ ...current, ...patch }))
  return (
    <div role="group" aria-label={`New variant for ${candidateLabel}`} className="callout stack-sm" onKeyDown={onKeyDown}>
      <div className="grid-form">
        <Field
          label={cli ? 'Model' : 'Deployment name'}
          hint={cli ? 'Any model id or alias the CLI accepts' : 'The Foundry deployment of the other model'}
          error={problem}
        >
          <input value={input.deployment} onChange={(e) => set({ deployment: e.target.value })} />
        </Field>
        <Field
          label="Model version"
          hint={cli ? 'Pins a snapshot, e.g. 20250805; empty uses the CLI default' : 'Recorded; the deployment fixes the version'}
        >
          <input value={input.modelVersion} onChange={(e) => set({ modelVersion: e.target.value })} />
        </Field>
        <Field label="Model name" hint="Used to warn when a judge grades its own model">
          <input value={input.modelName} onChange={(e) => set({ modelName: e.target.value })} />
        </Field>
        {!cli && (
          <Field label="Meter terms" hint="Model words in Azure meter names, to match this variant's rates">
            <input
              value={input.meterTerms}
              placeholder={source.meter_terms || undefined}
              onChange={(e) => set({ meterTerms: e.target.value })}
            />
          </Field>
        )}
        <Field label="Display name">
          <input value={input.name} placeholder={suggestedVariantName(fields)} onChange={(e) => set({ name: e.target.value })} />
        </Field>
      </div>
      <p className="small muted">
        Sends <code>{modelArgument(fields)}</code>. Saved as a new model profile with the settings of {source.name}. Rates are
        not copied: set them for the new model under Models &amp; pricing.
      </p>
      <ErrorMessage error={create.error} />
      <div className="row">
        <button type="button" className="primary small" disabled={create.isPending} onClick={save}>
          {create.isPending ? 'Saving…' : 'Save variant'}
        </button>
        <button type="button" className="small" onClick={onCancel}>
          Cancel
        </button>
      </div>
    </div>
  )
}
