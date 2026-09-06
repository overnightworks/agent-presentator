import { dirname } from 'node:path'
import { fileURLToPath } from 'node:url'

import { resolveOptions } from '@slidev/cli'
import { describe, expect, it } from 'vitest'

import addonManifest from '../package.json' with { type: 'json' }

const addonRoot = dirname(fileURLToPath(new URL('../package.json', import.meta.url)))
const exampleDeck = fileURLToPath(new URL('../../examples/hello-deck/slides.md', import.meta.url))

const loadExampleDeck = () => resolveOptions({ entry: exampleDeck }, 'build')

describe('the example deck loading the presentator addon', () => {
  it('names an addon root that Slidev resolves to this package', async () => {
    const { addonRoots } = await loadExampleDeck()

    expect(addonRoots).toEqual([addonRoot])
  })

  it('declares a Slidev engine range the installed CLI satisfies', async () => {
    // Slidev refuses an addon whose `engines.slidev` the running CLI is outside
    // of, so a deck that loads is the check that the range still holds.
    await expect(loadExampleDeck()).resolves.toBeDefined()
    expect(addonManifest.engines.slidev).toBeTruthy()
  })
})
