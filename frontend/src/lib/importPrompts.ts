/**
 * Read suite prompts from a CSV or JSONL file the user picked. Parsing happens in the browser, so nothing
 * is uploaded; the prompts land in the suite editor and are saved with the suite as usual.
 *
 * CSV: a header row with `user` (required) and optionally `title`, `system` and `reference`.
 * JSONL: one JSON object per line with `user` (or `prompt`), and optionally `title`, `system`,
 * `reference` (or `expected`) and `checks` (a list of check definitions, validated when the suite is saved).
 */

export interface ImportedPrompt {
  title: string
  system: string
  user: string
  reference: string
  checks: unknown[]
}

export interface ImportResult {
  prompts: ImportedPrompt[]
  errors: string[]
}

const MAX_PROMPTS = 500

/** RFC 4180 CSV: quoted fields may contain commas, quotes ("") and line breaks. */
export function parseCsv(text: string): string[][] {
  const rows: string[][] = []
  let row: string[] = []
  let field = ''
  let quoted = false
  for (let i = 0; i < text.length; i++) {
    const ch = text[i]
    if (quoted) {
      if (ch === '"' && text[i + 1] === '"') {
        field += '"'
        i++
      } else if (ch === '"') {
        quoted = false
      } else {
        field += ch
      }
    } else if (ch === '"' && field === '') {
      quoted = true
    } else if (ch === ',') {
      row.push(field)
      field = ''
    } else if (ch === '\n' || ch === '\r') {
      if (ch === '\r' && text[i + 1] === '\n') i++
      row.push(field)
      rows.push(row)
      row = []
      field = ''
    } else {
      field += ch
    }
  }
  if (field !== '' || row.length > 0) {
    row.push(field)
    rows.push(row)
  }
  return rows.filter((r) => r.some((cell) => cell.trim() !== ''))
}

function fromCsv(text: string): ImportResult {
  const rows = parseCsv(text.replace(/^﻿/, ''))
  if (rows.length === 0) return { prompts: [], errors: ['The file is empty.'] }
  const header = rows[0].map((h) => h.trim().toLowerCase())
  const column = (name: string) => header.indexOf(name)
  const user = column('user')
  if (user < 0) return { prompts: [], errors: ['The first row must name the columns, including "user".'] }
  const prompts: ImportedPrompt[] = []
  const errors: string[] = []
  rows.slice(1).forEach((row, index) => {
    const cell = (name: string) => {
      const at = column(name)
      return at >= 0 ? (row[at] ?? '') : ''
    }
    if (!(row[user] ?? '').trim()) {
      errors.push(`Row ${index + 2}: the user prompt is empty.`)
      return
    }
    prompts.push({ title: cell('title'), system: cell('system'), user: row[user], reference: cell('reference'), checks: [] })
  })
  return { prompts, errors }
}

function text(value: unknown): string {
  return typeof value === 'string' ? value : ''
}

function fromJsonl(content: string): ImportResult {
  const prompts: ImportedPrompt[] = []
  const errors: string[] = []
  content.split(/\r?\n/).forEach((line, index) => {
    if (!line.trim()) return
    let item: unknown
    try {
      item = JSON.parse(line)
    } catch {
      errors.push(`Line ${index + 1}: not valid JSON.`)
      return
    }
    if (!item || typeof item !== 'object' || Array.isArray(item)) {
      errors.push(`Line ${index + 1}: expected an object.`)
      return
    }
    const record = item as Record<string, unknown>
    const user = text(record.user) || text(record.prompt)
    if (!user.trim()) {
      errors.push(`Line ${index + 1}: "user" is missing or empty.`)
      return
    }
    if (record.checks !== undefined && !Array.isArray(record.checks)) {
      errors.push(`Line ${index + 1}: "checks" must be a list.`)
      return
    }
    prompts.push({
      title: text(record.title),
      system: text(record.system),
      user,
      reference: text(record.reference) || text(record.expected),
      checks: (record.checks as unknown[] | undefined) ?? [],
    })
  })
  return { prompts, errors }
}

/** Parse by file name: .csv as CSV, anything else (.jsonl, .json, .ndjson) as JSON lines. */
export function importPrompts(fileName: string, content: string, existing = 0): ImportResult {
  const result = fileName.toLowerCase().endsWith('.csv') ? fromCsv(content) : fromJsonl(content)
  const room = Math.max(0, MAX_PROMPTS - existing)
  if (result.prompts.length > room) {
    result.errors.push(`A suite holds at most ${MAX_PROMPTS} prompts; ${result.prompts.length - room} were left out.`)
    result.prompts = result.prompts.slice(0, room)
  }
  return result
}
