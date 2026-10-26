import type { ModelProfile, ProfileFields } from '../api/types'

interface ModelIdentity {
  api_style: string
  deployment: string
  model_version: string
}

export function isCli(apiStyle: string): boolean {
  return apiStyle === 'claude_cli' || apiStyle === 'codex_cli'
}

/**
 * The model sent to the provider, as the backend builds it. A local CLI takes any model id, so its version
 * pins a snapshot; a Foundry deployment fixes its own version, which is only recorded.
 */
export function requestModel(profile: ModelIdentity): string {
  const version = profile.model_version.trim()
  return isCli(profile.api_style) && version ? `${profile.deployment.trim()}-${version}` : profile.deployment
}

/** How the model reaches the provider, e.g. `--model claude-opus-4-1-20250805`. */
export function modelArgument(profile: ModelIdentity): string {
  const flag = profile.api_style === 'claude_cli' ? '--model' : profile.api_style === 'codex_cli' ? '-m' : 'model'
  return `${flag} ${requestModel(profile)}`
}

/** The model the provider says answered, when it says more than what was sent (an alias resolved, a dated version). */
export function reportedModel(profile: ModelIdentity, responseModel: string | null | undefined): string | null {
  if (!responseModel) return null
  return responseModel === requestModel(profile) || responseModel === profile.deployment ? null : responseModel
}

/** The model and version, which is what tells a variant apart (profile pickers already name the API). */
export function suggestedVariantName(profile: ModelIdentity): string {
  const version = profile.model_version.trim()
  return isCli(profile.api_style) || !version ? requestModel(profile) : `${profile.deployment.trim()} ${version}`
}

export interface VariantInput {
  deployment: string
  modelVersion: string
  modelName: string
  meterTerms: string
  name: string
}

/** The source's model and version, as the variant form starts from them. */
export function sameModel(source: ModelProfile, input: VariantInput): boolean {
  return input.deployment.trim() === source.deployment.trim() && input.modelVersion.trim() === source.model_version.trim()
}

/**
 * A copy of a profile with another model or version. Rates are never copied (they belong to the profile
 * and are matched or entered for the new model), and neither are the meter terms that find them.
 */
export function variantFields(source: ModelProfile, input: VariantInput): ProfileFields {
  const { id: _id, archived: _a, created_at: _c, updated_at: _u, ...fields } = source
  const next: ProfileFields = {
    ...fields,
    deployment: input.deployment.trim(),
    model_version: input.modelVersion.trim(),
    model_name: input.modelName.trim(),
    meter_terms: input.meterTerms.trim(),
    notes: `Variant of ${source.name}.`,
  }
  return { ...next, name: input.name.trim() || suggestedVariantName(next) }
}
