import { useQuery } from '@tanstack/react-query'

import { get } from './client'
import type {
  EndpointOut,
  EvaluationOut,
  ModelProfile,
  PresetCatalog,
  PricingStatus,
  RatesOut,
  Rubric,
  RunDetail,
  RunSummary,
  SettingsOut,
  Suite,
} from './types'

export const keys = {
  settings: ['settings'] as const,
  endpoints: ['endpoints'] as const,
  profiles: (archived = false) => ['profiles', archived] as const,
  presets: ['presets'] as const,
  rates: (profileId: string) => ['rates', profileId] as const,
  pricingStatus: ['pricing-status'] as const,
  runs: (filter = '') => ['runs', filter] as const,
  run: (runId: string) => ['run', runId] as const,
  suites: (archived = false) => ['suites', archived] as const,
  suite: (suiteId: string) => ['suite', suiteId] as const,
  rubrics: ['rubrics'] as const,
  evaluations: (runId: string) => ['evaluations', runId] as const,
}

export function useSettings() {
  return useQuery({ queryKey: keys.settings, queryFn: () => get<SettingsOut>('/api/settings') })
}

export function useEndpoints() {
  return useQuery({ queryKey: keys.endpoints, queryFn: () => get<EndpointOut[]>('/api/endpoints') })
}

export function useProfiles(includeArchived = false) {
  return useQuery({
    queryKey: keys.profiles(includeArchived),
    queryFn: () => get<ModelProfile[]>(`/api/profiles?include_archived=${includeArchived}`),
  })
}

export function usePresets() {
  return useQuery({ queryKey: keys.presets, queryFn: () => get<PresetCatalog>('/api/capability-presets'), staleTime: Infinity })
}

export function useRates(profileId: string | undefined) {
  return useQuery({
    queryKey: keys.rates(profileId ?? ''),
    queryFn: () => get<RatesOut>(`/api/profiles/${profileId}/rates`),
    enabled: Boolean(profileId),
  })
}

export function usePricingStatus() {
  return useQuery({ queryKey: keys.pricingStatus, queryFn: () => get<PricingStatus>('/api/pricing/status') })
}

export function useRuns(filter = '') {
  return useQuery({ queryKey: keys.runs(filter), queryFn: () => get<RunSummary[]>(`/api/runs${filter}`) })
}

export function useRun(runId: string | undefined) {
  return useQuery({
    queryKey: keys.run(runId ?? ''),
    queryFn: () => get<RunDetail>(`/api/runs/${runId}`),
    enabled: Boolean(runId),
    refetchInterval: (query) => {
      const data = query.state.data
      if (!data) return false
      const busy = data.active || data.run.status === 'running' || data.run.status === 'queued'
      return busy ? 1000 : false
    },
  })
}

export function useSuites(includeArchived = false) {
  return useQuery({
    queryKey: keys.suites(includeArchived),
    queryFn: () => get<Suite[]>(`/api/suites?include_archived=${includeArchived}`),
  })
}

export function useSuite(suiteId: string | undefined) {
  return useQuery({
    queryKey: keys.suite(suiteId ?? ''),
    queryFn: () => get<Suite>(`/api/suites/${suiteId}`),
    enabled: Boolean(suiteId),
  })
}

export function useRubrics() {
  return useQuery({ queryKey: keys.rubrics, queryFn: () => get<Rubric[]>('/api/rubrics') })
}

/** Evaluations of a run, polled while any of them is still queued or running. */
export function useEvaluations(runId: string | undefined) {
  return useQuery({
    queryKey: keys.evaluations(runId ?? ''),
    queryFn: () => get<EvaluationOut[]>(`/api/runs/${runId}/evaluations`),
    enabled: Boolean(runId),
    refetchInterval: (query) => {
      const data = query.state.data
      return data?.some((e) => e.status === 'queued' || e.status === 'running') ? 1000 : false
    },
  })
}
