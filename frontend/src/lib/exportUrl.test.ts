import { describe, expect, it } from 'vitest'

import { exportUrl } from './exportUrl'

describe('exportUrl', () => {
  it('points at the run export with the format and text choice', () => {
    expect(exportUrl('run-1', 'json', false)).toBe('/api/runs/run-1/export?format=json&include_text=false')
    expect(exportUrl('run-1', 'csv', true)).toBe('/api/runs/run-1/export?format=csv&include_text=true')
  })

  it('encodes the run id so it cannot change the path or the query', () => {
    const url = exportUrl('a/b?c=d', 'json', false)
    expect(url).toBe('/api/runs/a%2Fb%3Fc%3Dd/export?format=json&include_text=false')
  })
})
