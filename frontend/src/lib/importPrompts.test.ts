import { describe, expect, it } from 'vitest'

import { importPrompts, parseCsv } from './importPrompts'

describe('parseCsv', () => {
  it('handles quotes, escaped quotes, commas and line breaks inside fields', () => {
    const rows = parseCsv('title,user\r\n"A, b","He said ""hi""\nthen left"\nplain,second\n\n')
    expect(rows).toEqual([
      ['title', 'user'],
      ['A, b', 'He said "hi"\nthen left'],
      ['plain', 'second'],
    ])
  })
})

describe('importPrompts', () => {
  it('reads CSV by header name and reports empty user prompts by row', () => {
    const csv = '﻿Title,User,Reference,System\nCapital,What is the capital of France?,Paris,Be brief\nBlank,,x,\n'
    const result = importPrompts('prompts.csv', csv)
    expect(result.prompts).toEqual([
      { title: 'Capital', system: 'Be brief', user: 'What is the capital of France?', reference: 'Paris', checks: [] },
    ])
    expect(result.errors).toEqual(['Row 3: the user prompt is empty.'])
  })

  it('needs a user column in CSV', () => {
    expect(importPrompts('p.csv', 'question\nx\n').errors[0]).toMatch(/"user"/)
  })

  it('reads JSON lines with aliases and checks, and reports bad lines', () => {
    const jsonl = [
      '{"prompt": "2+2?", "expected": "4", "checks": [{"kind": "number", "value": "4"}]}',
      '',
      'not json',
      '[1, 2]',
      '{"user": "  "}',
      '{"user": "Hi", "checks": "contains"}',
    ].join('\n')
    const result = importPrompts('set.jsonl', jsonl)
    expect(result.prompts).toEqual([
      { title: '', system: '', user: '2+2?', reference: '4', checks: [{ kind: 'number', value: '4' }] },
    ])
    expect(result.errors).toEqual([
      'Line 3: not valid JSON.',
      'Line 4: expected an object.',
      'Line 5: "user" is missing or empty.',
      'Line 6: "checks" must be a list.',
    ])
  })

  it('keeps a suite within 500 prompts and says how many were left out', () => {
    const jsonl = Array.from({ length: 5 }, (_, i) => JSON.stringify({ user: `q${i}` })).join('\n')
    const result = importPrompts('a.jsonl', jsonl, 497)
    expect(result.prompts).toHaveLength(3)
    expect(result.errors).toEqual(['A suite holds at most 500 prompts; 2 were left out.'])
  })
})
