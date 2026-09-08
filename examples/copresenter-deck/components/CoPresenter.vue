<script setup>
import { computed, onMounted, onUnmounted, ref } from 'vue'
import { useNav } from '@slidev/client'

const { currentSlideNo } = useNav()

const LOCAL_COPRESENTER = 'http://127.0.0.1:3040'
const LANGUAGE = 'de'

function copresenterAddress() {
  if (typeof window === 'undefined') return LOCAL_COPRESENTER
  const asked = new URLSearchParams(window.location.search).get('copresenter')
  return asked || window.COPRESENTER_URL || LOCAL_COPRESENTER
}

const COPRESENTER = copresenterAddress()

const on = ref(false)
const heard = ref('')
const answer = ref('')
const hearing = ref('off')
const error = ref('')
const audioSeconds = ref(0)
const speaking = ref(false)
const whoModel = ref('')

let mediaStream = null
let audioContext = null
let workletNode = null
let hearSocket = null
let recognition = null
let askAbort = null
let playQueue = Promise.resolve()
let spokenText = ''
let sampleRate = 16000
let activationGeneration = 0
let playGeneration = 0
let activeAudio = null
let finishActivePlay = null

const state = computed(() => {
  if (!on.value) return 'off'
  if (error.value) return 'error'
  if (speaking.value) return 'speak'
  return 'listen'
})

function snapshot() {
  const tracks = mediaStream ? mediaStream.getTracks() : []
  return {
    on: on.value,
    heard: heard.value,
    answer: answer.value,
    hearing: hearing.value,
    audioSeconds: audioSeconds.value,
    error: error.value,
    model: whoModel.value,
    hearOpen: Boolean(hearSocket && hearSocket.readyState === WebSocket.OPEN),
    speaking: speaking.value,
    micLive: tracks.some((track) => track.readyState === 'live'),
    workletLive: Boolean(workletNode),
    audioPlaying: Boolean(activeAudio && !activeAudio.paused && !activeAudio.ended),
  }
}

function stillActive(generation) {
  return on.value && generation === activationGeneration
}

function setOn(value) {
  if (value) turnOn()
  else turnOff()
}

function say(text) {
  if (!on.value) return
  heard.value = text
  ask(text)
}

async function sendPcm(bytes) {
  if (!hearSocket || hearSocket.readyState !== WebSocket.OPEN) return false
  hearSocket.send(bytes)
  return true
}

function closeHear() {
  if (!hearSocket) return
  try { hearSocket.close() } catch { /* already closed */ }
}

async function turnOn() {
  if (on.value) return
  const generation = ++activationGeneration
  on.value = true
  error.value = ''
  heard.value = ''
  answer.value = ''
  audioSeconds.value = 0
  spokenText = ''
  let report
  try {
    const response = await fetch(`${COPRESENTER}/who`)
    if (!stillActive(generation)) return
    report = await response.json()
    if (!stillActive(generation)) return
    whoModel.value = report.answerer?.model || ''
    sampleRate = report.speech?.sample_rate || 16000
  } catch {
    if (!stillActive(generation)) return
    error.value = 'Unreachable'
    hearing.value = 'off'
    return
  }
  if (!stillActive(generation)) return
  if (report.speech?.hearing?.ready) {
    hearing.value = 'local'
    await startLocalHear(generation)
    return
  }
  if (startBrowserHear()) {
    hearing.value = 'browser'
    return
  }
  hearing.value = 'off'
  error.value = 'No hearing'
}

function turnOff() {
  activationGeneration += 1
  on.value = false
  hearing.value = 'off'
  error.value = ''
  if (askAbort) {
    askAbort.abort()
    askAbort = null
  }
  stopBrowserHear()
  releaseCapture()
  invalidatePlayback()
}

async function startLocalHear(generation) {
  const url = COPRESENTER.replace(/^http/, 'ws') + `/hear?language=${LANGUAGE}`
  const socket = new WebSocket(url)
  socket.binaryType = 'arraybuffer'
  bindHearSocket(socket, generation)
  if (!stillActive(generation)) {
    abandon({ socket })
    return
  }
  hearSocket = socket
  let stream = null
  let context = null
  let node = null
  try {
    stream = await navigator.mediaDevices.getUserMedia({
      audio: { echoCancellation: true, noiseSuppression: true, channelCount: 1 },
    })
    if (!stillActive(generation)) {
      abandon({ socket, stream })
      return
    }
    mediaStream = stream
    context = new AudioContext()
    audioContext = context
    await context.audioWorklet.addModule(workletUrl())
    if (!stillActive(generation)) {
      abandon({ socket, stream, context })
      return
    }
    const source = context.createMediaStreamSource(stream)
    node = new AudioWorkletNode(context, 'pcm-capture')
    workletNode = node
    const fromRate = context.sampleRate
    let pending = new Float32Array(0)
    node.port.onmessage = (event) => {
      if (!stillActive(generation)) return
      pending = concat(pending, event.data)
      const needed = Math.round(fromRate * 0.1)
      while (pending.length >= needed) {
        const frame = pending.subarray(0, needed)
        pending = pending.subarray(needed)
        if (hearSocket && hearSocket.readyState === WebSocket.OPEN) {
          hearSocket.send(toPcm16(frame, fromRate, sampleRate).buffer)
        }
      }
    }
    source.connect(node)
  } catch {
    abandon({ socket, stream, context, node })
    if (!stillActive(generation)) return
    fallbackHear('No hearing')
  }
}

function bindHearSocket(socket, generation) {
  socket.onmessage = (event) => {
    if (!stillActive(generation) || hearSocket !== socket) return
    let payload
    try {
      payload = JSON.parse(event.data)
    } catch {
      return
    }
    if (payload.error) {
      fallbackHear('No hearing')
      return
    }
    if (!payload.text) return
    heard.value = payload.text
    if (payload.final) onFinal(payload.text)
  }
  socket.onerror = () => {
    if (!stillActive(generation) || hearSocket !== socket) return
    fallbackHear('No hearing')
  }
  socket.onclose = () => {
    if (hearSocket === socket) hearSocket = null
    if (!stillActive(generation)) return
    fallbackHear('Hearing closed')
  }
}

function fallbackHear(message) {
  activationGeneration += 1
  releaseCapture()
  if (!on.value) return
  error.value = message
  if (startBrowserHear()) {
    hearing.value = 'browser'
    return
  }
  hearing.value = 'off'
}

function abandon({ socket, stream, context, node } = {}) {
  if (socket) {
    if (hearSocket === socket) hearSocket = null
    socket.onmessage = null
    socket.onerror = null
    socket.onclose = null
    try { socket.close() } catch { /* already closed */ }
  }
  if (node) {
    if (workletNode === node) workletNode = null
    node.port.onmessage = null
    try { node.disconnect() } catch { /* already disconnected */ }
  }
  if (context) {
    if (audioContext === context) audioContext = null
    try { context.close() } catch { /* already closed */ }
  }
  if (stream) {
    if (mediaStream === stream) mediaStream = null
    for (const track of stream.getTracks()) track.stop()
  }
}

function releaseCapture() {
  abandon({
    socket: hearSocket,
    stream: mediaStream,
    context: audioContext,
    node: workletNode,
  })
}

function startBrowserHear() {
  if (recognition) return true
  const Ctor = window.SpeechRecognition || window.webkitSpeechRecognition
  if (!Ctor) return false
  recognition = new Ctor()
  recognition.lang = 'de-DE'
  recognition.continuous = true
  recognition.interimResults = true
  recognition.onresult = (event) => {
    for (let i = event.resultIndex; i < event.results.length; i += 1) {
      const result = event.results[i]
      const text = result[0].transcript.trim()
      if (!text) continue
      heard.value = text
      if (result.isFinal) onFinal(text)
    }
  }
  recognition.onend = () => {
    if (on.value && recognition) {
      try { recognition.start() } catch { /* Chrome restarts this way */ }
    }
  }
  try {
    recognition.start()
    return true
  } catch {
    recognition = null
    return false
  }
}

function stopBrowserHear() {
  if (!recognition) return
  recognition.onresult = null
  recognition.onend = null
  try { recognition.stop() } catch { /* already stopped */ }
  recognition = null
}

function onFinal(text) {
  if (!on.value || speaking.value) return
  if (isEcho(text, spokenText)) return
  ask(text)
}

function isEcho(text, spoken) {
  if (!spoken) return false
  const words = (value) => new Set(value.toLowerCase().split(/\s+/).filter((w) => w.length > 2))
  const a = words(text)
  const b = words(spoken)
  if (!a.size) return false
  let overlap = 0
  for (const word of a) if (b.has(word)) overlap += 1
  return overlap / a.size >= 0.5
}

async function ask(text) {
  invalidatePlayback()
  if (askAbort) askAbort.abort()
  const controller = new AbortController()
  askAbort = controller
  answer.value = ''
  spokenText = ''
  try {
    const response = await fetch(`${COPRESENTER}/ask`, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ said: text, slide: currentSlideNo.value, language: LANGUAGE }),
      signal: controller.signal,
    })
    if (!response.ok || !response.body) {
      error.value = 'No answer'
      return
    }
    await readSse(response.body, controller.signal)
  } catch (err) {
    if (err.name === 'AbortError') return
    error.value = 'No answer'
  }
}

async function readSse(body, signal) {
  const reader = body.getReader()
  const decoder = new TextDecoder()
  let buffer = ''
  while (!signal.aborted) {
    const { done, value } = await reader.read()
    if (done) break
    buffer += decoder.decode(value, { stream: true })
    const parts = buffer.split('\n\n')
    buffer = parts.pop() || ''
    for (const block of parts) applySse(block)
  }
}

function applySse(block) {
  if (!on.value) return
  const kind = (block.match(/^event:\s*(\S+)/m) || [])[1]
  const dataLine = (block.match(/^data:\s*(.*)$/m) || [])[1]
  if (!kind || dataLine == null) return
  let data
  try { data = JSON.parse(dataLine) } catch { return }
  if (kind === 'text' && data.text) answer.value += data.text
  if (kind === 'sentence' && data.text) spokenText = `${spokenText} ${data.text}`.trim()
  if (kind === 'audio' && data.wav_b64) enqueueWav(data.wav_b64)
  if (kind === 'done' && data.text) answer.value = data.text
  if (kind === 'error') error.value = 'No answer'
}

function enqueueWav(b64) {
  if (!on.value) return
  const generation = playGeneration
  const bytes = Uint8Array.from(atob(b64), (c) => c.charCodeAt(0))
  const url = URL.createObjectURL(new Blob([bytes], { type: 'audio/wav' }))
  playQueue = playQueue.then(() => {
    if (generation !== playGeneration) return undefined
    return playUrl(url, generation)
  }).finally(() => URL.revokeObjectURL(url))
}

function playUrl(url, generation) {
  return new Promise((resolve) => {
    if (generation !== playGeneration) {
      resolve()
      return
    }
    const audio = new Audio(url)
    activeAudio = audio
    speaking.value = true
    let settled = false
    const settle = (failed) => {
      if (settled) return
      settled = true
      if (finishActivePlay === abortPlay) finishActivePlay = null
      if (activeAudio === audio) activeAudio = null
      if (generation !== playGeneration) {
        resolve()
        return
      }
      speaking.value = false
      if (failed) error.value = 'No speech'
      resolve()
    }
    const abortPlay = () => settle(false)
    finishActivePlay = abortPlay
    audio.onloadedmetadata = () => {
      if (generation !== playGeneration) return
      if (Number.isFinite(audio.duration) && audio.duration > 0) {
        audioSeconds.value += audio.duration
      }
    }
    audio.onended = () => settle(false)
    audio.onerror = () => settle(true)
    audio.play().then(() => {
      if (generation !== playGeneration) abortPlay()
    }).catch(() => settle(true))
  })
}

function stopActiveAudio() {
  const audio = activeAudio
  activeAudio = null
  if (!audio) return
  audio.onended = null
  audio.onerror = null
  audio.onloadedmetadata = null
  audio.pause()
  audio.removeAttribute('src')
  audio.load()
}

function invalidatePlayback() {
  playGeneration += 1
  stopActiveAudio()
  speaking.value = false
  if (finishActivePlay) {
    const finish = finishActivePlay
    finishActivePlay = null
    finish()
  }
  playQueue = Promise.resolve()
}

function workletUrl() {
  const source = `
    class PcmCapture extends AudioWorkletProcessor {
      process(inputs) {
        const channel = inputs[0] && inputs[0][0]
        if (channel) this.port.postMessage(new Float32Array(channel))
        return true
      }
    }
    registerProcessor('pcm-capture', PcmCapture)
  `
  return URL.createObjectURL(new Blob([source], { type: 'text/javascript' }))
}

function concat(left, right) {
  const out = new Float32Array(left.length + right.length)
  out.set(left)
  out.set(right, left.length)
  return out
}

function toPcm16(float32, fromRate, toRate) {
  const ratio = fromRate / toRate
  const length = Math.max(1, Math.round(float32.length / ratio))
  const out = new Int16Array(length)
  for (let i = 0; i < length; i += 1) {
    const sample = float32[Math.min(float32.length - 1, Math.floor(i * ratio))]
    const clamped = Math.max(-1, Math.min(1, sample))
    out[i] = clamped < 0 ? clamped * 0x8000 : clamped * 0x7fff
  }
  return out
}

onMounted(() => {
  window.__copresenter = { setOn, say, sendPcm, snapshot, closeHear }
})
onUnmounted(() => {
  turnOff()
  delete window.__copresenter
})
</script>

<template>
  <aside
    class="copresenter"
    :data-on="on"
    :data-hearing="hearing"
    :data-state="state"
    :data-audio-seconds="audioSeconds"
    :aria-label="on ? 'Presenter on' : 'Presenter off'"
  >
    <button
      class="switch"
      type="button"
      role="switch"
      :aria-checked="on"
      aria-label="Presenter"
      @click="setOn(!on)"
    >
      <span class="track" :data-on="on">
        <span class="thumb" />
      </span>
      <span class="label">Presenter</span>
    </button>
    <p v-if="on" class="status">
      <span class="glyph" :data-state="state" aria-hidden="true" />
      <span class="path">{{ hearing }}</span>
    </p>
    <p v-if="on && heard" class="line heard">{{ heard }}</p>
    <p v-if="on && answer" class="line answer">{{ answer }}</p>
    <p v-if="on && error" class="line fail">{{ error }}</p>
  </aside>
</template>

<style scoped>
.copresenter {
  position: fixed;
  right: 16px;
  bottom: 16px;
  z-index: 100;
  min-width: 140px;
  max-width: min(420px, calc(100vw - 32px));
  padding: 10px 12px;
  border-radius: 14px;
  background: color-mix(in srgb, canvas 82%, CanvasText 18%);
  color: CanvasText;
  box-shadow: 0 8px 24px color-mix(in srgb, CanvasText 25%, transparent);
  font: 14px/1.3 system-ui, sans-serif;
  pointer-events: auto;
}
.switch {
  display: flex;
  align-items: center;
  gap: 8px;
  padding: 0;
  border: 0;
  background: transparent;
  color: inherit;
  cursor: pointer;
  font: inherit;
}
.track {
  width: 36px;
  height: 20px;
  border-radius: 999px;
  background: #6b7280;
  position: relative;
  flex-shrink: 0;
}
.track[data-on="true"] { background: #059669; }
.thumb {
  position: absolute;
  top: 2px;
  left: 2px;
  width: 16px;
  height: 16px;
  border-radius: 50%;
  background: #fff;
  transition: transform 120ms linear;
}
.track[data-on="true"] .thumb { transform: translateX(16px); }
.label { font-weight: 650; }
.status {
  display: flex;
  align-items: center;
  gap: 8px;
  margin: 8px 0 0;
}
.glyph {
  width: 10px;
  height: 10px;
  border-radius: 50%;
  background: #059669;
  flex-shrink: 0;
}
.glyph[data-state="listen"] { animation: pulse 1.2s ease-in-out infinite; }
.glyph[data-state="speak"] {
  border-radius: 2px;
  width: 14px;
  height: 10px;
  background: repeating-linear-gradient(90deg, #059669 0 3px, transparent 3px 5px);
}
.glyph[data-state="error"] {
  border-radius: 2px;
  background: #d97706;
}
.path {
  font-size: 12px;
  letter-spacing: 0.04em;
  text-transform: lowercase;
  opacity: 0.7;
}
.line {
  margin: 6px 0 0;
  font-size: 13px;
}
.heard { font-style: italic; opacity: 0.8; }
.answer { font-weight: 550; }
.fail { color: #d97706; }
@media (prefers-reduced-motion: reduce) {
  .glyph[data-state="listen"] { animation: none; }
  .thumb { transition: none; }
}
@keyframes pulse {
  0%, 100% { opacity: 1; transform: scale(1); }
  50% { opacity: 0.45; transform: scale(0.85); }
}
</style>
