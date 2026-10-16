import { useEffect, useState } from 'react'

import { post } from './client'

/**
 * POST `body` to `path` shortly after it stops changing (for previews); `null` clears the result.
 *
 * `data` is the latest result, kept on screen while a newer request is pending. `current` says
 * whether it answers exactly the body passed now: a paid action confirmed from a preview must check
 * it, so a cost shown for an earlier scope or setting is never what the user agrees to.
 */
export function useDebouncedPost<T>(path: string | null, body: unknown, delay = 350) {
  const [result, setResult] = useState<{ request: string; data: T } | null>(null)
  const [error, setError] = useState<unknown>(null)
  const serialized = body === null || body === undefined || !path ? null : JSON.stringify([path, body])
  useEffect(() => {
    if (!path || serialized === null) {
      setResult(null)
      setError(null)
      return
    }
    let cancelled = false
    const timer = window.setTimeout(() => {
      post<T>(path, JSON.parse(serialized)[1])
        .then((data) => {
          if (!cancelled) {
            setResult({ request: serialized, data })
            setError(null)
          }
        })
        .catch((reason) => {
          if (!cancelled) setError(reason)
        })
    }, delay)
    return () => {
      cancelled = true
      window.clearTimeout(timer)
    }
  }, [path, serialized, delay])
  const current = result !== null && serialized !== null && result.request === serialized && !error
  return { data: result?.data ?? null, error, current }
}
