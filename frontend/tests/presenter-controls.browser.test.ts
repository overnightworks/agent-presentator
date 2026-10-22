import { createHash } from 'node:crypto'
import { createServer, type ServerResponse } from 'node:http'
import { copyFile, mkdir, mkdtemp, readFile, rm, stat, writeFile } from 'node:fs/promises'
import { tmpdir } from 'node:os'
import { dirname, extname, join, normalize, resolve } from 'node:path'
import { fileURLToPath } from 'node:url'
import { execFile } from 'node:child_process'
import { promisify } from 'node:util'

import { chromium, type ElementHandle, type Page } from 'playwright-chromium'
import { afterAll, afterEach, beforeAll, beforeEach, describe, expect, it } from 'vitest'

const run = promisify(execFile)
const repositoryRoot = fileURLToPath(new URL('../..', import.meta.url))
const frontendRoot = join(repositoryRoot, 'frontend')
const overlayDeckSourceDirectory = join(repositoryRoot, 'examples/copresenter-deck')
const exampleDeck = join(overlayDeckSourceDirectory, 'slides.md')
const providedBuild = process.env.PRESENTER_RETURN_BUILD_DIR
const evidenceDirectory = process.env.PRESENTER_RETURN_EVIDENCE_DIR
const wav = 'UklGRiQAAABXQVZFZm10IBAAAAABAAEAQB8AAEAfAAABAAgAZGF0YQAAAAA='
// Exactly what `examples/copresenter-deck/README.md` tells an operator to
// copy next to their own `slides.md` — proven by assembling a fresh deck from
// only these files, never the repository's own example directory, where
// every other file already happens to sit beside them regardless of whether
// the README's own list is complete.
const COPIED_OVERLAY_FILES = [
  'custom-nav-controls.vue',
  'style.css',
  'components/CoPresenter.vue',
  'public/pcm-capture-processor.js',
]

let buildDirectory = providedBuild ?? ''
let ownsBuildDirectory = false
let server: ReturnType<typeof createServer>
let origin = ''
let hearingOpened = 0
let hearingClosed = 0
let pendingAnswerClosed = 0
let answerMode: 'audio' | 'speech-refused' = 'audio'
let hearingMode: 'ready' | 'not-ready' | 'socket-error' | 'unreachable' = 'ready'
let productionSecurityHeaders: Record<string, string> = {}
type CspViolation = { directive: string; blockedUri: string }
let cspViolations: CspViolation[] = []
const hearingSockets = new Set<import('node:stream').Duplex>()

type MediaObservation = {
  microphoneOpened: boolean
  trackStopped: boolean
  playbackStarted: boolean
  playbackPaused: boolean
}

let media = createMediaObservation()

function frame(message: string) {
  const data = Buffer.from(message)
  return Buffer.concat([Buffer.from([0x81, data.length]), data])
}

function mimeType(pathname: string) {
  return {
    '.css': 'text/css',
    '.html': 'text/html',
    '.js': 'text/javascript',
    '.json': 'application/json',
    '.svg': 'image/svg+xml',
  }[extname(pathname)] ?? 'application/octet-stream'
}

// What `SecurityHeadersPolicy` really sends in production on the talk host,
// where a talk runs: the policy `presentator.api.talk.talk_content_security_policy`
// builds, read through this one bridge call and never hand-copied here, so
// a changed policy fails this test's own build rather than silently drifting
// from what a browser reaching the real deployment gets.
const PYTHON_SECURITY_HEADERS_SCRIPT = `
import json
from presentator.api.talk import talk_content_security_policy
print(json.dumps({"content-security-policy": talk_content_security_policy()}))
`

async function loadProductionSecurityHeaders(): Promise<Record<string, string>> {
  const { stdout } = await run('uv', ['run', 'python', '-c', PYTHON_SECURITY_HEADERS_SCRIPT], {
    cwd: repositoryRoot,
  })
  return JSON.parse(stdout) as Record<string, string>
}

function boxesIntersect(first: { x: number, y: number, width: number, height: number }, second: { x: number, y: number, width: number, height: number }) {
  return first.x < second.x + second.width
    && second.x < first.x + first.width
    && first.y < second.y + second.height
    && second.y < first.y + first.height
}

async function readSameFrameBoxes(page: Page, homeHandle: ElementHandle<Element>, aiHandle: ElementHandle<Element>) {
  return page.evaluate(([home, ai]) => {
    const rectOf = (element: Element) => {
      const { x, y, width, height } = element.getBoundingClientRect()
      return { x, y, width, height }
    }
    return {
      homeBox: rectOf(home),
      aiBox: rectOf(ai),
      navWidth: home.parentElement!.getBoundingClientRect().width,
    }
  }, [homeHandle, aiHandle] as const)
}

async function saveEvidence(page: Page, name: string) {
  if (!evidenceDirectory) return
  await page.screenshot({ path: join(evidenceDirectory, name), fullPage: true })
}

async function buildExample() {
  if (providedBuild) return
  buildDirectory = await mkdtemp(join(tmpdir(), 'presenter-controls-'))
  ownsBuildDirectory = true
  await run('pnpm', ['exec', 'slidev', 'build', exampleDeck, '--out', buildDirectory], {
    cwd: frontendRoot,
  })
}

// Builds a second, independent deck containing only the files
// `examples/copresenter-deck/README.md` names, plus the minimal `slides.md`
// every deck needs (the one file that is the operator's own, never copied).
async function buildCopiedOverlayDeck(): Promise<string> {
  const deckDirectory = await mkdtemp(join(tmpdir(), 'presenter-copied-overlay-deck-'))
  for (const relativePath of COPIED_OVERLAY_FILES) {
    const destination = join(deckDirectory, relativePath)
    await mkdir(dirname(destination), { recursive: true })
    await copyFile(join(overlayDeckSourceDirectory, relativePath), destination)
  }
  await writeFile(
    join(deckDirectory, 'slides.md'),
    '---\ntitle: Copied overlay\nfonts:\n  provider: none\nfavicon: false\n---\n\n# Copied overlay\n',
  )
  const outDirectory = await mkdtemp(join(tmpdir(), 'presenter-copied-overlay-build-'))
  await run(
    'pnpm',
    ['exec', 'slidev', 'build', join(deckDirectory, 'slides.md'), '--out', outDirectory],
    { cwd: frontendRoot },
  )
  await rm(deckDirectory, { recursive: true, force: true })
  return outDirectory
}

async function serveFile(directory: string, pathname: string, response: ServerResponse) {
  const requested = normalize(resolve(directory, `.${pathname}`))
  const candidate = requested.startsWith(`${directory}/`) ? requested : ''
  try {
    const info = candidate ? await stat(candidate) : null
    if (info?.isFile()) {
      response.writeHead(200, { 'content-type': mimeType(candidate), ...productionSecurityHeaders })
      response.end(await readFile(candidate))
      return
    }
  } catch {
    // The SPA fallback below owns client-side routes.
  }
  response.writeHead(200, { 'content-type': 'text/html', ...productionSecurityHeaders })
  response.end(await readFile(join(directory, 'index.html')))
}

// A peer reset while a page tears down is expected on an upgraded socket and
// would otherwise surface as an unhandled `error` that fails the whole run;
// any other socket error is kept for the shared `afterEach` to judge.
const socketErrors: Error[] = []

function recordUnexpectedSocketErrors(socket: import('node:stream').Duplex) {
  socket.on('error', (error: NodeJS.ErrnoException) => {
    if (error.code !== 'ECONNRESET') socketErrors.push(error)
  })
}

function websocketAcceptKey(key: string) {
  return createHash('sha1').update(`${key}258EAFA5-E914-47DA-95CA-C5AB0DC85B11`).digest('base64')
}

// The minimal server the copied-overlay proof needs: the static build plus
// the two routes `startLocalHear`'s own path calls before it ever reaches the
// AudioWorklet (`/who`, then the hearing lease). No `/ask` route and no
// observation beacons: this proof stops at the worklet loading, so it never
// drives a spoken turn.
async function createOverlayDeckServer(directory: string) {
  // `closeAllConnections` does not reach an upgraded (WebSocket) connection
  // (Node's own documented behaviour) — the same reason `startServer` below
  // tracks `hearingSockets` and destroys them itself before closing, which
  // this server has to do too or its own `close()` never calls back.
  const sockets = new Set<import('node:stream').Duplex>()
  const server = createServer(async (request, response) => {
    const url = new URL(request.url ?? '/', 'http://presenter.test')
    if (url.pathname === '/copresenter/who') {
      response.writeHead(200, { 'content-type': 'application/json' })
      response.end(
        JSON.stringify({
          speech: { sample_rate: 16000, hearing: { ready: true } },
        }),
      )
      return
    }
    await serveFile(directory, url.pathname, response)
  })
  server.on('upgrade', (request, socket) => {
    const key = request.headers['sec-websocket-key']
    if (!request.url?.startsWith('/copresenter/hear') || typeof key !== 'string') {
      socket.destroy()
      return
    }
    socket.write(
      'HTTP/1.1 101 Switching Protocols\r\nUpgrade: websocket\r\nConnection: Upgrade\r\n'
      + `Sec-WebSocket-Accept: ${websocketAcceptKey(key)}\r\n\r\n`,
    )
    recordUnexpectedSocketErrors(socket)
    sockets.add(socket)
    socket.on('close', () => sockets.delete(socket))
  })
  await new Promise<void>((resolve) => server.listen(0, '127.0.0.1', resolve))
  const address = server.address()
  if (!address || typeof address === 'string') {
    throw new Error('overlay deck server did not bind a TCP port')
  }
  return { server, origin: `http://127.0.0.1:${address.port}`, sockets }
}

async function startServer() {
  server = createServer(async (request, response) => {
    const url = new URL(request.url ?? '/', 'http://presenter.test')
    if (url.pathname === '/copresenter/who') {
      if (hearingMode === 'unreachable') {
        response.writeHead(503, { 'content-type': 'application/json' })
        response.end(JSON.stringify({ detail: 'Co-presenter unavailable' }))
        return
      }
      response.writeHead(200, { 'content-type': 'application/json' })
      response.end(JSON.stringify({ speech: { sample_rate: 16000, hearing: { ready: hearingMode !== 'not-ready' } } }))
      return
    }
    if (url.pathname === '/copresenter/ask') {
      response.writeHead(200, { 'content-type': 'text/event-stream' })
      if (answerMode === 'speech-refused') {
        const sentence = 'The handoff keeps this sentence.'
        response.write(`event: text\ndata: ${JSON.stringify({ text: sentence })}\n\n`)
        response.write(`event: sentence\ndata: ${JSON.stringify({ text: sentence })}\n\n`)
        response.end('event: error\ndata: {}\n\n')
        return
      }
      response.write(`event: audio\ndata: {"wav_b64":"${wav}"}\n\n`)
      request.on('close', () => { pendingAnswerClosed += 1 })
      return
    }
    if (request.method === 'POST' && url.pathname.startsWith('/observations/')) {
      if (url.pathname === '/observations/microphone-opened') media.microphoneOpened = true
      if (url.pathname === '/observations/track-stopped') media.trackStopped = true
      if (url.pathname === '/observations/playback-started') media.playbackStarted = true
      if (url.pathname === '/observations/playback-paused') media.playbackPaused = true
      response.writeHead(204)
      response.end()
      return
    }
    await serveFile(buildDirectory, url.pathname, response)
  })
  server.on('upgrade', (request, socket) => {
    if (!request.url?.startsWith('/copresenter/hear')) {
      socket.destroy()
      return
    }
    const key = request.headers['sec-websocket-key']
    if (typeof key !== 'string') {
      socket.destroy()
      return
    }
    hearingOpened += 1
    socket.write(
      'HTTP/1.1 101 Switching Protocols\r\nUpgrade: websocket\r\nConnection: Upgrade\r\n'
      + `Sec-WebSocket-Accept: ${websocketAcceptKey(key)}\r\n\r\n`,
    )
    if (hearingMode === 'ready') {
      socket.write(frame(JSON.stringify({ text: 'Where does the handoff slow down?', final: true })))
    }
    recordUnexpectedSocketErrors(socket)
    hearingSockets.add(socket)
    let closed = false
    const closeHearing = () => {
      if (closed) return
      closed = true
      hearingSockets.delete(socket)
      hearingClosed += 1
    }
    socket.on('data', (data) => {
      if ((data[0] & 0x0f) !== 0x08) return
      socket.end(Buffer.from([0x88, 0x00]))
    })
    socket.on('close', () => {
      closeHearing()
    })
  })
  await new Promise<void>((resolve) => server.listen(0, '127.0.0.1', resolve))
  const address = server.address()
  if (!address || typeof address === 'string') throw new Error('test server did not bind a TCP port')
  origin = `http://127.0.0.1:${address.port}`
}

function sendHearingError() {
  for (const socket of hearingSockets) socket.write(frame(JSON.stringify({ error: 'hearing failed' })))
}

function createMediaObservation(): MediaObservation {
  return {
    microphoneOpened: false,
    trackStopped: false,
    playbackStarted: false,
    playbackPaused: false,
  }
}

async function installMediaFakes(page: Page) {
  await page.addInitScript(() => {
    const report = (event: string) => {
      navigator.sendBeacon(`/observations/${event}`)
    }
    const track = {
      readyState: 'live',
      stop() {
        this.readyState = 'ended'
        report('track-stopped')
      },
    }
    const microphones = window as unknown as { __microphonesRequested: number }
    microphones.__microphonesRequested = 0
    Object.defineProperty(navigator, 'mediaDevices', {
      value: {
        getUserMedia: async () => {
          microphones.__microphonesRequested += 1
          report('microphone-opened')
          return { getTracks: () => [track] }
        },
      },
      configurable: true,
    })
    const RealAudioContext = window.AudioContext
    class FakeContext {
      sampleRate = 16000
      audioWorklet = {
        // Real, not faked: this is the one call the page's own CSP governs
        // (`script-src 'self'`), so delegating to a real, unconnected
        // `AudioContext`'s real `audioWorklet` proves the same-origin URL the
        // component names actually loads under that policy. Everything past
        // this point stays fake, because the component's own media graph
        // never has to be real for these tests to drive it.
        addModule: (moduleUrl: string) => new RealAudioContext().audioWorklet.addModule(moduleUrl),
      }
      createMediaStreamSource() { return { connect: () => undefined } }
      close() { return Promise.resolve() }
    }
    window.AudioContext = FakeContext as unknown as typeof AudioContext
    window.AudioWorkletNode = class {
      port = { onmessage: null }
      disconnect() {}
    } as unknown as typeof AudioWorkletNode
    const counter = window as unknown as { __speechRecognitionsBuilt: number }
    counter.__speechRecognitionsBuilt = 0
    class CountedSpeechRecognition {
      constructor() { counter.__speechRecognitionsBuilt += 1 }
      start() {}
      stop() {}
    }
    Object.assign(window, {
      SpeechRecognition: CountedSpeechRecognition,
      webkitSpeechRecognition: CountedSpeechRecognition,
    })
    HTMLMediaElement.prototype.play = async function () {
      Object.defineProperty(this, 'paused', { value: false, configurable: true })
      report('playback-started')
    }
    HTMLMediaElement.prototype.pause = function () {
      Object.defineProperty(this, 'paused', { value: true, configurable: true })
      report('playback-paused')
    }
  })
}

// Records every violation for the shared `afterEach` to judge: none is
// tolerated, so a page this built talk
// serves never silently drifts from what `script-src 'self'` and the rest of
// the production policy actually admit.
async function installCspGuard(page: Page) {
  await page.exposeFunction('__reportCspViolation', (directive: string, blockedUri: string) => {
    cspViolations.push({ directive, blockedUri })
  })
  await page.addInitScript(() => {
    document.addEventListener('securitypolicyviolation', (event) => {
      const reportViolation = (window as unknown as {
        __reportCspViolation: (directive: string, blockedUri: string) => void
      }).__reportCspViolation
      // `effectiveDirective` is the spec-recommended field (CSP3): unlike the
      // legacy `violatedDirective`, it never carries the matched source list,
      // so it compares equal to a bare directive name like 'img-src'.
      reportViolation(event.effectiveDirective, event.blockedURI)
    })
  })
}

async function openPresenterAt(base: string, path: string, width: number) {
  const browser = await chromium.launch({ headless: true })
  const page = await browser.newPage({ viewport: { width, height: 900 } })
  page.setDefaultTimeout(5_000)
  await installCspGuard(page)
  await installMediaFakes(page)
  await page.goto(`${base}${path}`, { waitUntil: 'domcontentloaded', timeout: 10_000 })
  return { browser, page }
}

async function openPresenter(width: number) {
  return openPresenterAt(origin, '/presenter/1', width)
}

beforeAll(async () => {
  await buildExample()
  productionSecurityHeaders = await loadProductionSecurityHeaders()
  await startServer()
}, 60_000)

afterAll(async () => {
  for (const socket of hearingSockets) socket.destroy()
  server.closeAllConnections()
  await new Promise<void>((resolve, reject) => server.close((error) => error ? reject(error) : resolve()))
  if (ownsBuildDirectory) await rm(buildDirectory, { recursive: true, force: true })
})

beforeEach(() => {
  cspViolations = []
  socketErrors.length = 0
})

afterEach(() => {
  expect(cspViolations).toEqual([])
  expect(socketErrors).toEqual([])
})

describe('presenter controls', () => {
  it.each([1024, 390])('keeps the admitted sentence visible when speech is refused during a switch at %ipx', async (width) => {
    answerMode = 'speech-refused'
    const { browser, page } = await openPresenter(width)
    try {
      const ai = page.getByRole('switch', { name: 'AI' })
      await ai.waitFor({ state: 'visible', timeout: 2_000 })
      await ai.click({ timeout: 2_000 })
      const sentence = page.getByText('The handoff keeps this sentence.')
      const refusal = page.getByText('No answer')
      await sentence.waitFor({ state: 'visible', timeout: 5_000 })
      await refusal.waitFor({ state: 'visible', timeout: 5_000 })
      expect(await sentence.isVisible()).toBe(true)
      expect(await refusal.isVisible()).toBe(true)
      await saveEvidence(page, `presenter-speech-refused-${width}.png`)
      await ai.click({ timeout: 2_000 })
      await expect.poll(() => hearingClosed).toBeGreaterThanOrEqual(hearingOpened)
    } finally {
      answerMode = 'audio'
      await browser.close()
    }
  })

  it.each([1024, 390])('keeps Home and AI separate and reachable at %ipx', async (width) => {
    const { browser, page } = await openPresenter(width)
    try {
      const home = page.getByRole('link', { name: 'Home' })
      const ai = page.getByRole('switch', { name: 'AI' })
      await home.waitFor({ state: 'visible', timeout: 2_000 })
      await ai.waitFor({ state: 'visible' })
      const { homeBox, aiBox, navWidth } = await readSameFrameBoxes(page, await home.elementHandle(), await ai.elementHandle())
      expect(homeBox.width).toBeGreaterThanOrEqual(44)
      expect(homeBox.height).toBeGreaterThanOrEqual(44)
      expect(aiBox.width).toBeGreaterThanOrEqual(44)
      expect(aiBox.height).toBeGreaterThanOrEqual(44)
      expect(
        boxesIntersect(homeBox, aiBox),
        `Home ${JSON.stringify(homeBox)} overlaps AI ${JSON.stringify(aiBox)} in a nav row ${navWidth}px wide`,
      ).toBe(false)
      await saveEvidence(page, `presenter-home-${width}.png`)

      await home.focus()
      await page.keyboard.press('Enter')
      await page.waitForURL(`${origin}/`)
    } finally {
      await browser.close()
    }
  })

  it('stops the active co-presenter before Home leaves and direct return starts off', async () => {
    media = createMediaObservation()
    const { browser, page } = await openPresenter(390)
    try {
      const ai = page.getByRole('switch', { name: 'AI' })
      await ai.waitFor({ state: 'visible', timeout: 2_000 })
      await ai.click({ timeout: 2_000 })
      await page.locator('.copresenter[data-state="speak"]').waitFor({ state: 'visible', timeout: 5_000 })
      await page.getByText('Where does the handoff slow down?').waitFor({ state: 'visible', timeout: 5_000 })
      expect(media.trackStopped).toBe(false)
      expect(media.playbackStarted).toBe(true)
      expect(media.playbackPaused).toBe(false)
      const [homeBox, aiBox, panelBox] = await Promise.all([
        page.getByRole('link', { name: 'Home' }).boundingBox(),
        ai.boundingBox(),
        page.locator('.copresenter .panel').boundingBox(),
      ])
      expect(homeBox).not.toBeNull()
      expect(aiBox).not.toBeNull()
      expect(panelBox).not.toBeNull()
      expect(panelBox!.width).toBeLessThanOrEqual(390 - 16)
      expect(panelBox!.height).toBeLessThanOrEqual(900 * 0.42)
      expect(boxesIntersect(homeBox!, panelBox!)).toBe(false)
      expect(boxesIntersect(aiBox!, panelBox!)).toBe(false)
      await saveEvidence(page, 'presenter-active-390.png')
      await page.getByRole('link', { name: 'Home' }).click({ timeout: 2_000 })
      await page.waitForURL(`${origin}/`)
      await expect.poll(() => hearingClosed).toBeGreaterThanOrEqual(hearingOpened)
      await expect.poll(() => pendingAnswerClosed).toBeGreaterThan(0)
      await expect.poll(() => media.trackStopped).toBe(true)
      await expect.poll(() => media.playbackPaused).toBe(true)

      await page.goto(`${origin}/presenter/1`, { waitUntil: 'domcontentloaded', timeout: 10_000 })
      expect(await page.getByRole('switch', { name: 'AI' }).getAttribute('aria-checked')).toBe('false')
    } finally {
      await browser.close()
    }
  })

  it.each([
    ['speech cannot be reached', 'unreachable', 'Unreachable'],
    ['local hearing is not ready', 'not-ready', 'Local hearing not ready'],
    ['the hearing socket sends an error', 'socket-error', 'No hearing'],
  ] as const)('says so and builds no browser speech recognition when %s', async (_situation, mode, message) => {
    hearingMode = mode
    media = createMediaObservation()
    const hearingOpenedBefore = hearingOpened
    const { browser, page } = await openPresenter(1024)
    try {
      const ai = page.getByRole('switch', { name: 'AI' })
      await ai.waitFor({ state: 'visible', timeout: 2_000 })
      await ai.click({ timeout: 2_000 })
      if (mode === 'socket-error') {
        // The error must land after activation finished: an error during
        // `addModule` is released by activation's own cleanup and would not
        // prove the error path releases the microphone.
        await expect.poll(() => page.evaluate(
          () => (window as unknown as {
            __copresenter: { snapshot: () => { workletLive: boolean } }
          }).__copresenter.snapshot().workletLive,
        )).toBe(true)
        sendHearingError()
      }
      await page.getByText(message).waitFor({ state: 'visible', timeout: 5_000 })
      expect(await page.locator('.copresenter').getAttribute('data-hearing')).toBe('off')
      expect(await page.evaluate(
        () => (window as unknown as { __speechRecognitionsBuilt: number }).__speechRecognitionsBuilt,
      )).toBe(0)
      await expect.poll(() => hearingClosed).toBeGreaterThanOrEqual(hearingOpened)
      if (mode === 'unreachable') expect(hearingOpened).toBe(hearingOpenedBefore)
      if (mode === 'socket-error') await expect.poll(() => media.trackStopped).toBe(true)
      else {
        expect(await page.evaluate(
          () => (window as unknown as { __microphonesRequested: number }).__microphonesRequested,
        )).toBe(0)
      }
    } finally {
      hearingMode = 'ready'
      await browser.close()
    }
  })

  it('does not put presenter controls on the projector', async () => {
    const browser = await chromium.launch({ headless: true })
    const page = await browser.newPage({ viewport: { width: 1024, height: 900 } })
    page.setDefaultTimeout(5_000)
    await installCspGuard(page)
    try {
      await page.goto(`${origin}/1`, { waitUntil: 'domcontentloaded', timeout: 10_000 })
      expect(await page.getByRole('link', { name: 'Home' }).count()).toBe(0)
      expect(await page.getByRole('switch', { name: 'AI' }).count()).toBe(0)
      await saveEvidence(page, 'projector.png')
    } finally {
      await browser.close()
    }
  })

  it('loads the AudioWorklet from exactly the README\'s copied overlay', async () => {
    const overlayBuildDirectory = await buildCopiedOverlayDeck()
    const { server: overlayServer, origin: overlayOrigin, sockets: overlaySockets }
      = await createOverlayDeckServer(overlayBuildDirectory)
    try {
      const { browser, page } = await openPresenterAt(overlayOrigin, '/presenter/1', 1024)
      try {
        const ai = page.getByRole('switch', { name: 'AI' })
        await ai.waitFor({ state: 'visible', timeout: 2_000 })
        await ai.click({ timeout: 2_000 })
        // `hearing` reaches 'local' optimistically, before
        // `AudioContext.audioWorklet.addModule` - real, not faked
        // (installMediaFakes) - is even awaited (CoPresenter.vue), so a test
        // that only waits for that attribute can pass while the worklet is
        // still loading or a CSP block is still in flight. `workletLive`
        // (snapshot()) is true only once the worklet node actually exists,
        // which happens after `addModule` resolved against the copied
        // overlay's own `public/pcm-capture-processor.js` under the
        // production CSP; a blocked or missing module throws there instead
        // and the component says it has no hearing.
        await page.waitForFunction(
          () => (window as unknown as {
            __copresenter: { snapshot: () => { workletLive: boolean } }
          }).__copresenter.snapshot().workletLive,
          undefined,
          { timeout: 5_000 },
        )
        const snapshot = await page.evaluate(
          () => (window as unknown as {
            __copresenter: { snapshot: () => { hearing: string } }
          }).__copresenter.snapshot(),
        )
        expect(snapshot.hearing).toBe('local')
        expect(await page.getByText('No hearing').count()).toBe(0)
      } finally {
        await browser.close()
      }
    } finally {
      for (const socket of overlaySockets) socket.destroy()
      overlayServer.closeAllConnections()
      await new Promise<void>((resolve, reject) => {
        overlayServer.close((error) => (error ? reject(error) : resolve()))
      })
      await rm(overlayBuildDirectory, { recursive: true, force: true })
    }
  }, 60_000)
})
