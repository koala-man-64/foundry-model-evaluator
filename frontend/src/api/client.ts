/**
 * Fetch wrapper for the local API. State-changing requests carry the headers the backend's
 * request guard requires (JSON body + X-FME-Client). API keys are sent only to the key endpoint
 * and are never stored in the browser.
 */

export class ApiError extends Error {
  status: number
  body: unknown

  constructor(status: number, message: string, body: unknown) {
    super(message)
    this.status = status
    this.body = body
  }
}

type Method = 'GET' | 'POST' | 'PUT' | 'PATCH' | 'DELETE'

interface RequestOptions {
  body?: unknown
  idempotencyKey?: string
  signal?: AbortSignal
}

function messageFrom(body: unknown, status: number): string {
  if (body && typeof body === 'object' && 'detail' in body) {
    const detail = (body as { detail: unknown }).detail
    if (typeof detail === 'string') return detail
    if (Array.isArray(detail)) {
      return detail
        .map((item) => {
          if (item && typeof item === 'object' && 'msg' in item) {
            const loc = Array.isArray((item as { loc?: unknown[] }).loc)
              ? (item as { loc: unknown[] }).loc.filter((p) => p !== 'body').join('.')
              : ''
            return `${loc ? `${loc}: ` : ''}${String((item as { msg: unknown }).msg)}`
          }
          return String(item)
        })
        .join('; ')
    }
  }
  return `Request failed (HTTP ${status})`
}

export async function api<T>(method: Method, path: string, options: RequestOptions = {}): Promise<T> {
  const headers: Record<string, string> = { Accept: 'application/json' }
  if (method !== 'GET') headers['X-FME-Client'] = '1'
  if (options.body !== undefined) headers['Content-Type'] = 'application/json'
  if (options.idempotencyKey) headers['Idempotency-Key'] = options.idempotencyKey
  const response = await fetch(path, {
    method,
    headers,
    body: options.body !== undefined ? JSON.stringify(options.body) : undefined,
    credentials: 'same-origin',
    cache: 'no-store',
    signal: options.signal,
  })
  if (response.status === 204) return undefined as T
  const text = await response.text()
  let body: unknown = null
  if (text) {
    try {
      body = JSON.parse(text)
    } catch {
      body = text
    }
  }
  if (!response.ok) throw new ApiError(response.status, messageFrom(body, response.status), body)
  return body as T
}

export const get = <T>(path: string, signal?: AbortSignal) => api<T>('GET', path, { signal })
export const post = <T>(path: string, body?: unknown, idempotencyKey?: string) =>
  api<T>('POST', path, { body: body ?? {}, idempotencyKey })
export const put = <T>(path: string, body: unknown) => api<T>('PUT', path, { body })
export const patch = <T>(path: string, body: unknown) => api<T>('PATCH', path, { body })
export const del = <T>(path: string) => api<T>('DELETE', path)

export function newIdempotencyKey(): string {
  return crypto.randomUUID()
}
