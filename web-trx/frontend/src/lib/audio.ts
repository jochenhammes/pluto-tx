/**
 * Web Audio glue for the RX/TX audio channels (see ws.ts / backend
 * protocol.py's RX_AUDIO / TX_AUDIO binary frames). Both directions run in
 * AudioWorklets (worklets/*.worklet.js) on the audio rendering thread, so
 * main-thread work (waterfall drawing, UI updates) can neither make capture
 * bursty nor make playback stutter.
 */
import micWorkletUrl from "./worklets/mic-capture.worklet.js?url";
import rxWorkletUrl from "./worklets/rx-player.worklet.js?url";

export interface PlayerStats {
  bufferedMs: number;
  underruns: number;
  droppedMs: number;
}

/** RX playback through a jitter-buffering worklet (rx-player.worklet.js).
 * Chunks pushed before the worklet has loaded are queued, not lost. */
export class AudioPlayer {
  private ctx: AudioContext | null = null;
  private node: AudioWorkletNode | null = null;
  private gain: GainNode | null = null;
  private volume = 1;
  private pending: Array<{ pcm16: Int16Array; rate: number }> = [];

  onStats: (s: PlayerStats) => void = () => {};

  /** 0 = silent, 1 = as received, up to 2 = +6 dB. Local to this browser. */
  setVolume(volume: number): void {
    this.volume = volume;
    if (this.gain && this.ctx) this.gain.gain.setTargetAtTime(volume, this.ctx.currentTime, 0.02);
  }

  ensureStarted(): void {
    if (!this.ctx) {
      const ctx = new AudioContext();
      this.ctx = ctx;
      void ctx.audioWorklet.addModule(rxWorkletUrl).then(() => {
        if (this.ctx !== ctx) return; // stopped meanwhile
        const node = new AudioWorkletNode(ctx, "rx-player", { outputChannelCount: [1] });
        node.port.onmessage = (e) => this.onStats(e.data as PlayerStats);
        const gain = ctx.createGain();
        gain.gain.value = this.volume;
        node.connect(gain).connect(ctx.destination);
        this.node = node;
        this.gain = gain;
        for (const p of this.pending) this.post(p.pcm16, p.rate);
        this.pending = [];
      });
    }
    if (this.ctx.state === "suspended") void this.ctx.resume();
  }

  push(pcm16: Int16Array, sampleRateHz: number): void {
    this.ensureStarted();
    if (this.node) this.post(pcm16, sampleRateHz);
    else if (this.pending.length < 50) this.pending.push({ pcm16, rate: sampleRateHz });
  }

  private post(pcm16: Int16Array, rate: number): void {
    this.node!.port.postMessage({ pcm16, rate }, [pcm16.buffer]);
  }

  stop(): void {
    this.node?.disconnect();
    this.gain?.disconnect();
    void this.ctx?.close();
    this.ctx = null;
    this.node = null;
    this.gain = null;
    this.pending = [];
  }
}

/** Captures the microphone and delivers 20 ms PCM16 blocks via `onChunk`
 * (mic-capture.worklet.js). The caller decides what to send -- see
 * App.svelte: only while PTT is held and the TX mode takes audio. */
export class MicCapture {
  private ctx: AudioContext | null = null;
  private stream: MediaStream | null = null;
  private node: AudioWorkletNode | null = null;
  private source: MediaStreamAudioSourceNode | null = null;

  /** captureTime: the block's position on the AudioContext clock -- lets
   * the caller drop blocks recorded BEFORE PTT was pressed. */
  onChunk: (pcm16: Int16Array, sampleRateHz: number, captureTime: number) => void = () => {};

  async start(): Promise<void> {
    if (this.ctx) return;
    this.stream = await navigator.mediaDevices.getUserMedia({ audio: true });
    // 48 kHz = the TX flowgraph's own rate, so the backend needs no
    // resampling (it still copes with other rates, see backend tx_audio.py).
    let ctx: AudioContext;
    try {
      ctx = new AudioContext({ sampleRate: 48000 });
    } catch {
      ctx = new AudioContext();
    }
    this.ctx = ctx;
    await ctx.audioWorklet.addModule(micWorkletUrl);
    this.source = ctx.createMediaStreamSource(this.stream);
    this.node = new AudioWorkletNode(ctx, "mic-capture", { numberOfOutputs: 0 });
    this.node.port.onmessage = (e) => {
      const { pcm16, captureTime } = e.data as { pcm16: Int16Array; captureTime: number };
      this.onChunk(pcm16, ctx.sampleRate, captureTime);
    };
    this.source.connect(this.node);
  }

  stop(): void {
    this.node?.disconnect();
    this.source?.disconnect();
    this.stream?.getTracks().forEach((t) => t.stop());
    void this.ctx?.close();
    this.node = null;
    this.source = null;
    this.stream = null;
    this.ctx = null;
  }

  get currentTime(): number {
    return this.ctx?.currentTime ?? 0;
  }

  get isActive(): boolean {
    return this.ctx !== null;
  }
}
