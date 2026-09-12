import { createHash } from 'node:crypto'
import { createServer, type ServerResponse } from 'node:http'
import { mkdtemp, readFile, rm, stat } from 'node:fs/promises'
import { tmpdir } from 'node:os'
import { extname, join, normalize, resolve } from 'node:path'
import { fileURLToPath } from 'node:url'
import { execFile } from 'node:child_process'
import { promisify } from 'node:util'

import { chromium, type Page } from 'playwright-chromium'
import { afterAll, beforeAll, describe, expect, it } from 'vitest'

const run = promisify(execFile)
const repositoryRoot = fileURLToPath(new URL('../..', import.meta.url))
const frontendRoot = join(repositoryRoot, 'frontend')
const exampleDeck = join(repositoryRoot, 'examples/copresenter-deck/slides.md')
const providedBuild = process.env.PRESENTER_RETURN_BUILD_DIR
const evidenceDirectory = process.env.PRESENTER_RETURN_EVIDENCE_DIR
const wav = 'UklGRiQAAABXQVZFZm10IBAAAAABAAEAQB8AAEAfAAABAAgAZGF0YQAAAAA='

let buildDirectory = providedBuild ?? ''
let ownsBuildDirectory = false
let server: ReturnType<typeof createServer>
let origin = ''
let hearingOpened = 0
let hearingClosed = 0
let pendingAnswerClosed = 0
const hearingSockets = new Set<import('node:stream').Duplex>()

type MediaObservation = {
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

function boxesIntersect(first: { x: number, y: number, width: number, height: number }, second: { x: number, y: number, width: number, height: number }) {
  return first.x < second.x + second.width
    && second.x < first.x + first.width
    && first.y < second.y + second.height
    && second.y < first.y + first.height
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

async function serveFile(pathname: string, response: ServerResponse) {
  const requested = normalize(resolve(buildDirectory, `.${pathname}`))
  const candidate = requested.startsWith(`${buildDirectory}/`) ? requested : ''
  try {
    const info = candidate ? await stat(candidate) : null
    if (info?.isFile()) {
      response.writeHead(200, { 'content-type': mimeType(candidate) })
      response.end(await readFile(candidate))
      return
    }
  } catch {
    // The SPA fallback below owns client-side routes.
  }
  response.writeHead(200, { 'content-type': 'text/html' })
  response.end(await readFile(join(buildDirectory, 'index.html')))
}

async function startServer() {
  server = createServer(async (request, response) => {
    const url = new URL(request.url ?? '/', 'http://presenter.test')
    if (url.pathname === '/copresenter/who') {
      response.writeHead(200, { 'content-type': 'application/json' })
      response.end(JSON.stringify({ answerer: { model: 'fake' }, speech: { sample_rate: 16000, hearing: { ready: true } } }))
      return
    }
    if (url.pathname === '/copresenter/ask') {
      response.writeHead(200, { 'content-type': 'text/event-stream' })
      response.write(`event: audio\ndata: {"wav_b64":"${wav}"}\n\n`)
      request.on('close', () => { pendingAnswerClosed += 1 })
      return
    }
    if (request.method === 'POST' && url.pathname.startsWith('/observations/')) {
      if (url.pathname === '/observations/track-stopped') media.trackStopped = true
      if (url.pathname === '/observations/playback-started') media.playbackStarted = true
      if (url.pathname === '/observations/playback-paused') media.playbackPaused = true
      response.writeHead(204)
      response.end()
      return
    }
    await serveFile(url.pathname, response)
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
    const accept = createHash('sha1').update(`${key}258EAFA5-E914-47DA-95CA-C5AB0DC85B11`).digest('base64')
    socket.write(`HTTP/1.1 101 Switching Protocols\r\nUpgrade: websocket\r\nConnection: Upgrade\r\nSec-WebSocket-Accept: ${accept}\r\n\r\n`)
    socket.write(frame(JSON.stringify({ text: 'Where does the handoff slow down?', final: true })))
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

function createMediaObservation(): MediaObservation {
  return {
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
    Object.defineProperty(navigator, 'mediaDevices', {
      value: { getUserMedia: async () => ({ getTracks: () => [track] }) },
      configurable: true,
    })
    class FakeContext {
      sampleRate = 16000
      audioWorklet = { addModule: async () => undefined }
      createMediaStreamSource() { return { connect: () => undefined } }
      close() { return Promise.resolve() }
    }
    window.AudioContext = FakeContext as unknown as typeof AudioContext
    window.AudioWorkletNode = class {
      port = { onmessage: null }
      disconnect() {}
    } as unknown as typeof AudioWorkletNode
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

async function openPresenter(width: number) {
  const browser = await chromium.launch({ headless: true })
  const page = await browser.newPage({ viewport: { width, height: 900 } })
  page.setDefaultTimeout(5_000)
  await installMediaFakes(page)
  await page.goto(`${origin}/presenter/1`, { waitUntil: 'domcontentloaded', timeout: 10_000 })
  return { browser, page }
}

beforeAll(async () => {
  await buildExample()
  await startServer()
}, 60_000)

afterAll(async () => {
  for (const socket of hearingSockets) socket.destroy()
  server.closeAllConnections()
  await new Promise<void>((resolve, reject) => server.close((error) => error ? reject(error) : resolve()))
  if (ownsBuildDirectory) await rm(buildDirectory, { recursive: true, force: true })
})

describe('presenter controls', () => {
  it.each([1024, 390])('keeps Home and AI separate and reachable at %ipx', async (width) => {
    const { browser, page } = await openPresenter(width)
    try {
      const home = page.getByRole('link', { name: 'Home' })
      const ai = page.getByRole('switch', { name: 'AI' })
      await home.waitFor({ state: 'visible', timeout: 2_000 })
      await ai.waitFor({ state: 'visible' })
      const [homeBox, aiBox] = await Promise.all([home.boundingBox(), ai.boundingBox()])
      expect(homeBox).not.toBeNull()
      expect(aiBox).not.toBeNull()
      expect(homeBox!.width).toBeGreaterThanOrEqual(44)
      expect(homeBox!.height).toBeGreaterThanOrEqual(44)
      expect(aiBox!.width).toBeGreaterThanOrEqual(44)
      expect(aiBox!.height).toBeGreaterThanOrEqual(44)
      expect(homeBox!.x + homeBox!.width <= aiBox!.x || aiBox!.x + aiBox!.width <= homeBox!.x).toBe(true)
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

  it('does not put presenter controls on the projector', async () => {
    const browser = await chromium.launch({ headless: true })
    const page = await browser.newPage({ viewport: { width: 1024, height: 900 } })
    page.setDefaultTimeout(5_000)
    try {
      await page.goto(`${origin}/1`, { waitUntil: 'domcontentloaded', timeout: 10_000 })
      expect(await page.getByRole('link', { name: 'Home' }).count()).toBe(0)
      expect(await page.getByRole('switch', { name: 'AI' }).count()).toBe(0)
      await saveEvidence(page, 'projector.png')
    } finally {
      await browser.close()
    }
  })
})
