import { useEffect, useState } from 'react'

import { post } from './client'

/** POST `body` to `path` shortly after it stops changing (for previews); `null` clears the result. */
export function useDebouncedPost<T>(path: string | null, body: unknown, delay = 350) {
  const [data, setData] = useState<T | null>(null)
  const [error, setError] = useState<unknown>(null)
  const serialized = body === null || body === undefined ? null : JSON.stringify(body)
  useEffect(() => {
    if (!path || serialized === null) {
      setData(null)
      setError(null)
      return
    }
    let cancelled = false
    const timer = window.setTimeout(() => {
      post<T>(path, JSON.parse(serialized))
        .then((result) => {
          if (!cancelled) {
            setData(result)
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
  return { data, error }
}
