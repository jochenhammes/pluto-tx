// RX audio playback with a jitter buffer, on the audio rendering thread.
// The main thread posts PCM chunks as they arrive over the WebSocket
// (bursty); this processor plays them out at a steady rate:
// - playback starts once TARGET_S of audio is queued (absorbs network jitter)
//   and restarts the same way after an underrun, instead of stuttering;
// - more than MAX_S queued (e.g. after a network stall) drops the oldest
//   audio, so latency can't keep growing;
// - input at another rate (the server sends 16 kHz) is linearly resampled
//   to the context rate.
// Reports its state (buffered ms, underruns, dropped ms) about once a second.
const TARGET_S = 0.12;
const MAX_S = 0.4;

class RxPlayerProcessor extends AudioWorkletProcessor {
  constructor() {
    super();
    this.capacity = Math.ceil(sampleRate * 2);
    this.ring = new Float32Array(this.capacity);
    this.read = 0;
    this.count = 0;
    this.playing = false;
    this.underruns = 0;
    this.dropped = 0;
    this.sinceReport = 0;
    this.port.onmessage = (e) => {
      if (e.data.reset) {
        this.read = this.count = 0;
        this.playing = false;
        return;
      }
      this.push(e.data.pcm16, e.data.rate);
    };
  }

  push(pcm16, rate) {
    const ratio = rate / sampleRate; // input samples per output sample
    const n = Math.floor(pcm16.length / ratio);
    for (let i = 0; i < n; i++) {
      const pos = i * ratio;
      const i0 = Math.floor(pos);
      const i1 = Math.min(i0 + 1, pcm16.length - 1);
      const f = pos - i0;
      const v = (pcm16[i0] * (1 - f) + pcm16[i1] * f) / 32768;
      this.ring[(this.read + this.count) % this.capacity] = v;
      if (this.count < this.capacity) this.count++;
      else this.read = (this.read + 1) % this.capacity;
    }
    const max = Math.floor(MAX_S * sampleRate);
    if (this.count > max) {
      const drop = this.count - max;
      this.read = (this.read + drop) % this.capacity;
      this.count -= drop;
      this.dropped += drop;
    }
  }

  process(_inputs, outputs) {
    const out = outputs[0][0];
    if (!this.playing && this.count >= TARGET_S * sampleRate) this.playing = true;
    for (let i = 0; i < out.length; i++) {
      if (this.playing && this.count > 0) {
        out[i] = this.ring[this.read];
        this.read = (this.read + 1) % this.capacity;
        this.count--;
      } else {
        if (this.playing) {
          this.playing = false;
          this.underruns++;
        }
        out[i] = 0;
      }
    }
    for (let c = 1; c < outputs[0].length; c++) outputs[0][c].set(out);
    this.sinceReport += out.length;
    if (this.sinceReport >= sampleRate) {
      this.sinceReport = 0;
      this.port.postMessage({
        bufferedMs: Math.round((this.count / sampleRate) * 1000),
        underruns: this.underruns,
        droppedMs: Math.round((this.dropped / sampleRate) * 1000),
      });
    }
    return true;
  }
}

registerProcessor("rx-player", RxPlayerProcessor);
