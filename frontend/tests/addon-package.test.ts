import { dirname } from 'node:path'
import { fileURLToPath } from 'node:url'

import { resolveOptions } from '@slidev/cli'
import { describe, expect, it } from 'vitest'

import addonManifest from '../package.json' with { type: 'json' }

const addonRoot = dirname(fileURLToPath(new URL('../package.json', import.meta.url)))
const fixtureDeck = fileURLToPath(new URL('./fixtures/addon-fixture.md', import.meta.url))

const loadFixtureDeck = () => resolveOptions({ entry: fixtureDeck }, 'build')

describe('the presentator addon package', () => {
  it('names an addon root that Slidev resolves to this package', async () => {
    const { addonRoots } = await loadFixtureDeck()

    expect(addonRoots).toEqual([addonRoot])
  })

  it('declares a Slidev engine range the installed CLI satisfies', async () => {
    // Slidev refuses an addon whose `engines.slidev` the running CLI is outside
    // of, so a deck that loads is the check that the range still holds.
    await expect(loadFixtureDeck()).resolves.toBeDefined()
    expect(addonManifest.engines.slidev).toBeTruthy()
  })
})
