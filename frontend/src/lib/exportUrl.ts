export type ExportFormat = 'json' | 'csv'

/** The download URL of a run export. A same-origin GET: the browser saves the file the server names. */
export function exportUrl(runId: string, format: ExportFormat, includeText: boolean): string {
  const query = new URLSearchParams({ format, include_text: includeText ? 'true' : 'false' })
  return `/api/runs/${encodeURIComponent(runId)}/export?${query.toString()}`
}
