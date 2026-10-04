// The co-presenter's own AudioWorklet processor, served same-origin so the
// shared Content-Security-Policy's `script-src 'self'` admits it: a `blob:`
// module URL built from this same source is not `'self'`, and broadening
// the policy to admit one would widen every page under it.
class PcmCapture extends AudioWorkletProcessor {
  process(inputs) {
    const channel = inputs[0] && inputs[0][0]
    if (channel) this.port.postMessage(new Float32Array(channel))
    return true
  }
}

registerProcessor('pcm-capture', PcmCapture)
