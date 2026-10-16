export function formatTokens(value: number | null | undefined): string {
  if (value === null || value === undefined) return 'not reported'
  return value.toLocaleString('en-US')
}

export function formatMs(value: number | null | undefined): string {
  if (value === null || value === undefined) return '—'
  if (value < 1000) return `${value} ms`
  return `${(value / 1000).toFixed(value < 10000 ? 2 : 1)} s`
}

export function formatDate(value: string | null | undefined): string {
  if (!value) return '—'
  const date = new Date(value)
  if (Number.isNaN(date.getTime())) return value
  return date.toLocaleString(undefined, { dateStyle: 'medium', timeStyle: 'short' })
}

export function shortDate(value: string | null | undefined): string {
  if (!value) return '—'
  const date = new Date(value)
  if (Number.isNaN(date.getTime())) return value
  return date.toLocaleDateString(undefined, { dateStyle: 'medium' })
}

export const API_STYLE_LABELS: Record<string, string> = {
  openai_responses: 'OpenAI Responses',
  openai_chat: 'Chat Completions',
  anthropic_messages: 'Claude Messages',
}

export const CATEGORY_LABELS: Record<string, string> = {
  input_uncached: 'Uncached input',
  cache_read: 'Cache read',
  cache_write: 'Cache write',
  cache_write_5m: 'Cache write (5 min)',
  cache_write_1h: 'Cache write (1 hour)',
  output: 'Output',
}

export const SOURCE_LABELS: Record<string, string> = {
  azure_auto: 'Azure (auto)',
  azure_pinned: 'Azure (pinned)',
  manual: 'Manual',
  custom_override: 'Custom rate',
  not_applicable: 'Not applicable',
}

export const STATUS_TONES: Record<string, string> = {
  succeeded: 'ok',
  completed: 'ok',
  complete: 'ok',
  failed: 'error',
  completed_with_errors: 'warn',
  cancelled: 'neutral',
  interrupted: 'warn',
  skipped_budget: 'warn',
  queued: 'info',
  running: 'info',
  incomplete: 'warn',
  not_sent: 'neutral',
  malformed: 'error',
  rejected_oversized: 'warn',
}

export const STATUS_LABELS: Record<string, string> = {
  succeeded: 'Succeeded',
  completed: 'Completed',
  completed_with_errors: 'Completed with errors',
  failed: 'Failed',
  cancelled: 'Cancelled',
  interrupted: 'Interrupted',
  skipped_budget: 'Skipped (budget)',
  queued: 'Queued',
  running: 'Running',
  malformed: 'Malformed output',
  rejected_oversized: 'Rejected (too large)',
}

export const FINISH_LABELS: Record<string, string> = {
  completed: 'Completed',
  max_tokens: 'Stopped at max tokens',
  content_filter: 'Content filtered',
  refusal: 'Refused',
  context_exceeded: 'Context exceeded',
  tool_call: 'Asked to call a tool',
  error: 'Error',
  other: 'Other',
}
