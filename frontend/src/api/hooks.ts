import { useQuery } from '@tanstack/react-query'

import { get } from './client'
import type {
  EndpointOut,
  ModelProfile,
  PresetCatalog,
  PricingStatus,
  RatesOut,
  RunDetail,
  RunSummary,
  SettingsOut,
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
