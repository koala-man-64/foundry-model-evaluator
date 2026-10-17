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
const KNOWN_FIELDS = new Set(['title', 'system', 'user', 'prompt', 'reference', 'expected', 'checks'])
const CHECK_KINDS = new Set(['contains', 'not_contains', 'exact', 'regex', 'number', 'json_schema'])

const CHECK_FIELDS: Record<string, string[]> = {
  contains: ['kind', 'value', 'case_sensitive'],
  not_contains: ['kind', 'value', 'case_sensitive'],
  exact: ['kind', 'value', 'case_sensitive'],
  regex: ['kind', 'pattern', 'case_sensitive'],
  number: ['kind', 'value', 'tolerance', 'which'],
  json_schema: ['kind', 'schema'],
}

function exactNumber(value: unknown): boolean {
  return typeof value === 'string' || (typeof value === 'number' && Number.isSafeInteger(value))
}

/** Why a check from a file cannot be used as written, or null. Nothing is coerced, defaulted or dropped. */
export function checkProblem(check: unknown): string | null {
  if (!check || typeof check !== 'object' || Array.isArray(check)) return 'a check must be an object'
  const c = check as Record<string, unknown>
  if (typeof c.kind !== 'string' || !CHECK_KINDS.has(c.kind))
    return `unknown check kind ${JSON.stringify(c.kind)} (use ${[...CHECK_KINDS].join(', ')})`
  const unknown = Object.keys(c).filter((key) => !CHECK_FIELDS[c.kind as string].includes(key))
  if (unknown.length) return `unknown field${unknown.length === 1 ? '' : 's'} ${unknown.join(', ')} for a ${c.kind} check`
  if ('case_sensitive' in c && typeof c.case_sensitive !== 'boolean') return 'case_sensitive must be true or false'
  if (c.kind === 'json_schema') {
    if (!c.schema || typeof c.schema !== 'object' || Array.isArray(c.schema)) return 'a json_schema check needs a schema object'
  } else if (c.kind === 'number') {
    if (!exactNumber(c.value)) return 'give a number check value as a string (or a whole number), so it stays exact'
    if ('tolerance' in c && !exactNumber(c.tolerance)) return 'give the tolerance as a string (or a whole number)'
    if ('which' in c && c.which !== 'last' && c.which !== 'any') return 'which must be "last" or "any"'
  } else if (c.kind === 'regex') {
    if (typeof c.pattern !== 'string' || !c.pattern) return 'a regex check needs a pattern'
  } else if (typeof c.value !== 'string' || !c.value) {
    return `a ${c.kind} check needs a text value`
  }
  return null
}

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
  // Columns that are not read: unknown names, and repeats of a known one (only the first is read).
  const unknown = header.filter((h, at) => h && (!['title', 'system', 'user', 'reference'].includes(h) || header.indexOf(h) !== at))
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
      errors.push(`Record ${index + 1} after the header: the user prompt is empty.`)
      return
    }
    prompts.push({ title: cell('title'), system: cell('system'), user: row[user], reference: cell('reference'), checks: [] })
  })
  if (unknown.length) errors.push(`Ignored columns: ${unknown.join(', ')}.`)
  return { prompts, errors }
}

/** Text fields accept strings, and numbers or booleans (written as text); anything else is reported. */
function text(value: unknown, field: string, problems: string[]): string {
  if (value === undefined || value === null) return ''
  if (typeof value === 'string') return value
  if (typeof value === 'number' || typeof value === 'boolean') return String(value)
  problems.push(`"${field}" must be text`)
  return ''
}

function fromJsonl(content: string): ImportResult {
  const prompts: ImportedPrompt[] = []
  const errors: string[] = []
  const ignored = new Set<string>()
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
    const problems: string[] = []
    const user = text(record.user, 'user', problems) || text(record.prompt, 'prompt', problems)
    if (!user.trim()) problems.push('"user" is missing or empty')
    if (record.checks !== undefined && !Array.isArray(record.checks)) problems.push('"checks" must be a list')
    const checks = Array.isArray(record.checks) ? record.checks : []
    checks.forEach((check, at) => {
      const problem = checkProblem(check)
      if (problem) problems.push(`check ${at + 1}: ${problem}`)
    })
    const fields = {
      title: text(record.title, 'title', problems),
      system: text(record.system, 'system', problems),
      reference: text(record.reference, 'reference', problems) || text(record.expected, 'expected', problems),
    }
    for (const key of Object.keys(record)) if (!KNOWN_FIELDS.has(key)) ignored.add(key)
    if (problems.length) {
      errors.push(`Line ${index + 1}: ${problems.join('; ')}.`)
      return
    }
    prompts.push({ ...fields, user, checks })
  })
  if (ignored.size) errors.push(`Ignored fields: ${[...ignored].sort().join(', ')}.`)
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
