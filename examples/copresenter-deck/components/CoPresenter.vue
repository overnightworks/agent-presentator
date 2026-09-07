<script setup>
import { computed, onMounted, onUnmounted, ref } from 'vue'
import { useNav } from '@slidev/client'

const { currentSlideNo } = useNav()

const COPRESENTER = (
  typeof window === 'undefined'
    ? ''
    : new URLSearchParams(window.location.search).get('copresenter')
) || 'http://127.0.0.1:3040'
const LANGUAGE = 'de'

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

const state = computed(() => {
  if (!on.value) return 'off'
  if (error.value) return 'error'
  if (speaking.value) return 'speak'
  return 'listen'
})

function snapshot() {
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
  }
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

async function turnOn() {
  if (on.value) return
  on.value = true
  error.value = ''
  heard.value = ''
  answer.value = ''
  audioSeconds.value = 0
  spokenText = ''
  let report
  try {
    const response = await fetch(`${COPRESENTER}/who`)
    report = await response.json()
    whoModel.value = report.answerer?.model || ''
    sampleRate = report.speech?.sample_rate || 16000
  } catch {
    error.value = 'Unreachable'
    hearing.value = 'off'
    return
  }
  if (report.speech?.hearing?.ready) {
    hearing.value = 'local'
    await startLocalHear()
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
  on.value = false
  hearing.value = 'off'
  speaking.value = false
  error.value = ''
  if (askAbort) {
    askAbort.abort()
    askAbort = null
  }
  stopLocalHear()
  stopBrowserHear()
  stopAudio()
}

async function startLocalHear() {
  const url = COPRESENTER.replace(/^http/, 'ws') + `/hear?language=${LANGUAGE}`
  hearSocket = new WebSocket(url)
  hearSocket.binaryType = 'arraybuffer'
  hearSocket.onmessage = (event) => {
    let payload
    try {
      payload = JSON.parse(event.data)
    } catch {
      return
    }
    if (!payload.text) return
    heard.value = payload.text
    if (payload.final) onFinal(payload.text)
  }
  hearSocket.onerror = () => {
    if (!on.value) return
    if (startBrowserHear()) hearing.value = 'browser'
    else error.value = 'No hearing'
  }
  try {
    mediaStream = await navigator.mediaDevices.getUserMedia({
      audio: { echoCancellation: true, noiseSuppression: true, channelCount: 1 },
    })
    audioContext = new AudioContext()
    await audioContext.audioWorklet.addModule(workletUrl())
    const source = audioContext.createMediaStreamSource(mediaStream)
    workletNode = new AudioWorkletNode(audioContext, 'pcm-capture')
    const fromRate = audioContext.sampleRate
    let pending = new Float32Array(0)
    workletNode.port.onmessage = (event) => {
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
    source.connect(workletNode)
  } catch {
    // Mic refused: the socket still accepts injected frames and `say()`.
  }
}

function stopLocalHear() {
  if (hearSocket) {
    hearSocket.onmessage = null
    hearSocket.onerror = null
    hearSocket.close()
    hearSocket = null
  }
  if (workletNode) {
    workletNode.port.onmessage = null
    workletNode.disconnect()
    workletNode = null
  }
  if (audioContext) {
    audioContext.close()
    audioContext = null
  }
  if (mediaStream) {
    for (const track of mediaStream.getTracks()) track.stop()
    mediaStream = null
  }
}

function startBrowserHear() {
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
  const bytes = Uint8Array.from(atob(b64), (c) => c.charCodeAt(0))
  const url = URL.createObjectURL(new Blob([bytes], { type: 'audio/wav' }))
  playQueue = playQueue.then(() => playUrl(url)).finally(() => URL.revokeObjectURL(url))
}

function playUrl(url) {
  return new Promise((resolve) => {
    const audio = new Audio(url)
    speaking.value = true
    audio.onloadedmetadata = () => {
      if (Number.isFinite(audio.duration) && audio.duration > 0) {
        audioSeconds.value += audio.duration
      }
    }
    const settle = () => {
      speaking.value = false
      resolve()
    }
    audio.onended = settle
    audio.onerror = settle
    audio.play().catch(settle)
  })
}

function stopAudio() {
  playQueue = Promise.resolve()
  speaking.value = false
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
  window.__copresenter = { setOn, say, sendPcm, snapshot }
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
