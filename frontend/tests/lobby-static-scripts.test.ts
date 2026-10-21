// @vitest-environment jsdom
import { afterEach, describe, expect, it, vi } from 'vitest'

async function runScript(script: string, markup: string) {
  document.body.innerHTML = markup
  vi.resetModules()
  await import(`../../src/presentator/api/static/${script}.js`)
}

function element<T extends HTMLElement>(selector: string) {
  const found = document.querySelector<T>(selector)
  if (!found) throw new Error(`missing ${selector}`)
  return found
}

afterEach(() => {
  vi.useRealTimers()
  vi.unstubAllGlobals()
  vi.restoreAllMocks()
})

describe('copy-value.js', () => {
  const markup = (value: string) => `<p><code>${value}</code><button type="button" data-copy>Copy</button></p>`

  function stubClipboard(writeText: (value: string) => Promise<void>) {
    const clipboard = { writeText: vi.fn(writeText) }
    vi.stubGlobal('navigator', { clipboard })
    return clipboard
  }

  it('copies the value shown before the button', async () => {
    const clipboard = stubClipboard(async () => {})
    await runScript('copy-value', markup('ssh-ed25519 AAAA deploy'))

    element('[data-copy]').click()

    expect(clipboard.writeText).toHaveBeenCalledWith('ssh-ed25519 AAAA deploy')
  })

  it('copies nothing when there is no value', async () => {
    const clipboard = stubClipboard(async () => {})
    await runScript('copy-value', markup(''))

    element('[data-copy]').click()

    expect(clipboard.writeText).not.toHaveBeenCalled()
  })

  it('reports a refused clipboard write instead of dropping it', async () => {
    const refusal = new Error('clipboard denied')
    stubClipboard(async () => { throw refusal })
    const report = vi.spyOn(console, 'error').mockImplementation(() => {})
    await runScript('copy-value', markup('value'))

    element('[data-copy]').click()

    await vi.waitFor(() => expect(report).toHaveBeenCalledWith('Copying to the clipboard failed', refusal))
  })
})

describe('source-new.js', () => {
  const markup = (proven: boolean) => `
    <input type="radio" name="access" value="ssh">
    <input type="radio" name="access" value="https" checked>
    <button type="submit" data-create data-proven="${proven}" disabled>Create</button>`

  it.each([
    { proven: true, disabled: false },
    { proven: false, disabled: true },
  ])('changing access leaves create disabled=$disabled when proven=$proven', async ({ proven, disabled }) => {
    await runScript('source-new', markup(proven))
    const ssh = element<HTMLInputElement>('input[value="ssh"]')

    ssh.checked = true
    ssh.dispatchEvent(new Event('change'))

    expect(element<HTMLButtonElement>('[data-create]').disabled).toBe(disabled)
  })
})

describe('history-back.js', () => {
  const markup = '<a href="/login" data-history-back>Back</a>'

  function stubHistory(length: number) {
    const history = { length, back: vi.fn() }
    vi.stubGlobal('history', history)
    return history
  }

  function clickBack() {
    // A cancelable click returns false from dispatchEvent when the default was prevented.
    return !element('[data-history-back]').dispatchEvent(new MouseEvent('click', { bubbles: true, cancelable: true }))
  }

  it('goes back in the history instead of following the link when there is a page to return to', async () => {
    const history = stubHistory(2)
    await runScript('history-back', markup)

    const navigationPrevented = clickBack()

    expect(history.back).toHaveBeenCalledOnce()
    expect(navigationPrevented).toBe(true)
  })

  it('follows the link when the history holds only this page', async () => {
    const history = stubHistory(1)
    await runScript('history-back', markup)

    const navigationPrevented = clickBack()

    expect(history.back).not.toHaveBeenCalled()
    expect(navigationPrevented).toBe(false)
  })
})

describe('voice-sample.js', () => {
  const markup = `
    <section data-sample-player data-voice="qwen3-tts-0.6b" data-play="Play" data-stop="Stop" data-loading="Loading" data-waiting="Waiting">
      <button type="button" data-sample-language="de" aria-pressed="true">Deutsch</button>
      <button type="button" data-sample-language="en" aria-pressed="false">English</button>
      <span data-sample-status hidden></span>
      <button type="button" data-sample-action>Play</button>
      <p data-sample-failure hidden>Failed</p>
    </section>`

  class FakeAudio extends EventTarget {
    static playback: () => Promise<void> = async () => {}
    static last: FakeAudio | undefined
    paused = true
    constructor(readonly src: string) {
      super()
      FakeAudio.last = this
    }
    async play() {
      await FakeAudio.playback()
      this.paused = false
    }
    pause() {
      this.paused = true
    }
  }

  type Answer = { status: number, ok: boolean, blob: () => Promise<Blob> }
  const answer = (status: number): Answer => ({ status, ok: status < 300, blob: async () => new Blob(['wav']) })

  function stubBrowser(answers: Array<Answer | 'hang'>) {
    const fetch = vi.fn((_url: string, init: RequestInit) => {
      const next = answers.shift() ?? 'hang'
      if (next !== 'hang') return Promise.resolve(next)
      return new Promise<Answer>((_resolve, reject) => {
        init.signal?.addEventListener('abort', () => reject(new DOMException('aborted', 'AbortError')))
      })
    })
    vi.stubGlobal('fetch', fetch)
    vi.stubGlobal('Audio', FakeAudio)
    vi.stubGlobal('URL', class extends URL {
      static createObjectURL = vi.fn(() => 'blob:sample')
      static revokeObjectURL = vi.fn()
    })
    FakeAudio.playback = async () => {}
    FakeAudio.last = undefined
    return fetch
  }

  const action = () => element<HTMLButtonElement>('[data-sample-action]')
  const status = () => element('[data-sample-status]')
  const failure = () => element('[data-sample-failure]')

  it('plays the sample in the chosen language and offers to stop it', async () => {
    const fetch = stubBrowser([answer(200)])
    await runScript('voice-sample', markup)

    element('[data-sample-language="en"]').click()
    action().click()

    await vi.waitFor(() => expect(action().textContent).toBe('Stop'))
    expect(fetch).toHaveBeenCalledWith('/settings/voice/qwen3-tts-0.6b/sample/en', expect.objectContaining({ method: 'POST' }))
    expect(element('[data-sample-language="en"]').getAttribute('aria-pressed')).toBe('true')
    expect(element('[data-sample-language="de"]').getAttribute('aria-pressed')).toBe('false')
    expect(FakeAudio.last?.paused).toBe(false)
    expect(status().hidden).toBe(true)
  })

  it('stops a playing sample on the second click', async () => {
    stubBrowser([answer(200)])
    await runScript('voice-sample', markup)
    action().click()
    await vi.waitFor(() => expect(action().textContent).toBe('Stop'))
    const playing = FakeAudio.last

    action().click()

    expect(playing?.paused).toBe(true)
    expect(action().textContent).toBe('Play')
    expect(URL.revokeObjectURL).toHaveBeenCalledWith('blob:sample')
  })

  it('returns to play when the sample ends', async () => {
    stubBrowser([answer(200)])
    await runScript('voice-sample', markup)
    action().click()
    await vi.waitFor(() => expect(action().textContent).toBe('Stop'))

    FakeAudio.last?.dispatchEvent(new Event('ended'))

    expect(action().textContent).toBe('Play')
    expect(action().disabled).toBe(false)
  })

  it('waits while the voice is busy and retries until the sample plays', async () => {
    vi.useFakeTimers()
    const fetch = stubBrowser([answer(409), answer(200)])
    await runScript('voice-sample', markup)

    action().click()
    await vi.waitFor(() => expect(status().textContent).toBe('Waiting'))
    expect(action().disabled).toBe(true)
    await vi.advanceTimersByTimeAsync(1000)

    await vi.waitFor(() => expect(action().textContent).toBe('Stop'))
    expect(fetch).toHaveBeenCalledTimes(2)
  })

  it.each([
    { case: 'the server refuses the sample', answers: [answer(500)], playback: async () => {} },
    { case: 'the browser cannot play it', answers: [answer(200)], playback: async () => { throw new Error('blocked') } },
  ])('shows the failure when $case', async ({ answers, playback }) => {
    stubBrowser(answers)
    FakeAudio.playback = playback
    await runScript('voice-sample', markup)

    action().click()

    await vi.waitFor(() => expect(failure().hidden).toBe(false))
    expect(action().textContent).toBe('Play')
    expect(action().disabled).toBe(false)
  })

  it('leaves the page quietly while a sample is loading', async () => {
    stubBrowser(['hang'])
    await runScript('voice-sample', markup)
    action().click()
    expect(status().textContent).toBe('Loading')

    window.dispatchEvent(new Event('pagehide'))
    await new Promise((settled) => setTimeout(settled, 0))

    expect(failure().hidden).toBe(true)
  })

  it('leaves the page quietly before any sample was requested', async () => {
    stubBrowser([])
    await runScript('voice-sample', markup)

    expect(() => window.dispatchEvent(new Event('pagehide'))).not.toThrow()
    expect(failure().hidden).toBe(true)
  })
})
