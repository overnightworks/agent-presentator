import { existsSync } from 'node:fs'
import { fileURLToPath } from 'node:url'

import { describe, expect, it } from 'vitest'

import manifest from '../package.json' with { type: 'json' }

describe('the presentator addon package', () => {
  it('carries the keywords by which Slidev recognises an addon', () => {
    expect(manifest.keywords).toEqual(expect.arrayContaining(['slidev-addon', 'slidev']))
  })

  it('resolves the Slidev that builds a deck using it', () => {
    expect(existsSync(fileURLToPath(import.meta.resolve('@slidev/cli')))).toBe(true)
  })
})
