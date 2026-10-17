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
    expect(result.errors).toEqual(['Record 2 after the header: the user prompt is empty.'])
  })

  it('lists CSV columns it does not use', () => {
    expect(importPrompts('p.csv', ['user,answer', 'q,a', ''].join('\n')).errors).toEqual(['Ignored columns: answer.'])
  })

  it('refuses checks it would otherwise have to guess at, and keeps numeric references', () => {
    const jsonl = [
      '{"user": "6*3?", "reference": 18, "answer": "18"}',
      '{"user": "a", "checks": [{"kind": "equals", "value": "x"}]}',
      '{"user": "b", "checks": [{"kind": "contains", "value": "x", "case_sensitive": "false"}]}',
      '{"user": "c", "checks": [{"kind": "number", "value": 0.1}]}',
      '{"user": "d", "title": {"x": 1}}',
    ].join('\n')
    const result = importPrompts('set.jsonl', jsonl)
    expect(result.prompts).toEqual([{ title: '', system: '', user: '6*3?', reference: '18', checks: [] }])
    expect(result.errors).toEqual([
      'Line 2: check 1: unknown check kind "equals" (use contains, not_contains, exact, regex, number, json_schema).',
      'Line 3: check 1: case_sensitive must be true or false.',
      'Line 4: check 1: give a number check value as a string (or a whole number), so it stays exact.',
      'Line 5: "title" must be text.',
      'Ignored fields: answer.',
    ])
  })

  it('refuses unknown or malformed check fields instead of dropping or defaulting them', () => {
    const jsonl = [
      '{"user": "a", "checks": [{"kind": "number", "value": "1", "which": "first"}]}',
      '{"user": "b", "checks": [{"kind": "contains", "value": "X", "casesensitive": true}]}',
      '{"user": "c", "checks": [{"kind": "number", "value": "1", "tolerance": null}]}',
      '{"user": "e", "checks": [{"kind": "regex", "value": "x"}]}',
      '{"user": "d", "checks": [{"kind": "number", "value": "1", "tolerance": "0.5", "which": "any"}]}',
      '{"user": "f", "checks": [{"kind": "regex", "pattern": ""}]}',
      '{"user": "g", "checks": [{"kind": "regex", "pattern": "^[0-9]+$", "case_sensitive": true}]}',
    ].join('\n')
    const result = importPrompts('set.jsonl', jsonl)
    expect(result.prompts.map((p) => p.user)).toEqual(['d', 'g'])
    expect(result.errors).toEqual([
      'Line 1: check 1: which must be "last" or "any".',
      'Line 2: check 1: unknown field casesensitive for a contains check.',
      'Line 3: check 1: give the tolerance as a string (or a whole number).',
      'Line 4: check 1: unknown field value for a regex check.',
      'Line 6: check 1: a regex check needs a pattern.',
    ])
  })

  it('lists a repeated CSV column as ignored', () => {
    expect(importPrompts('p.csv', ['user,User,title', 'q,r,t', ''].join('\n')).errors).toEqual(['Ignored columns: user.'])
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
