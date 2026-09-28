// Microphone capture on the audio rendering thread (AudioWorklet), replacing
// the old ScriptProcessorNode on the main thread: busy main-thread work
// (waterfall drawing, Svelte updates) can no longer delay or bunch up
// capture. Posts 20 ms blocks of 16-bit PCM plus the block's position on the
// AudioContext clock -- the main thread uses that to drop blocks recorded
// before PTT was pressed (see App.svelte, mic.onChunk).
const BLOCK_S = 0.02;

class MicCaptureProcessor extends AudioWorkletProcessor {
  constructor() {
    super();
    this.blockLen = Math.round(sampleRate * BLOCK_S);
    this.block = new Int16Array(this.blockLen);
    this.fill = 0;
    this.blockStart = 0;
  }

  process(inputs) {
    const channel = inputs[0] && inputs[0][0];
    if (!channel) return true;
    for (let i = 0; i < channel.length; i++) {
      if (this.fill === 0) this.blockStart = currentTime + i / sampleRate;
      const s = Math.max(-1, Math.min(1, channel[i]));
      this.block[this.fill++] = s < 0 ? s * 32768 : s * 32767;
      if (this.fill === this.blockLen) {
        const out = this.block;
        this.port.postMessage({ pcm16: out, captureTime: this.blockStart }, [out.buffer]);
        this.block = new Int16Array(this.blockLen);
        this.fill = 0;
      }
    }
    return true;
  }
}

registerProcessor("mic-capture", MicCaptureProcessor);
