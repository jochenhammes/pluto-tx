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

/** Where TX audio comes from (docs/BROWSER_AUDIO.md): an input device of this computer -- microphone, line-in,
 * USB sound card, virtual cable -- or an audio file. processing: the browser's echo cancellation, noise
 * suppression and automatic gain (fine for a microphone, harmful for a line signal). */
export type TxSource =
  | { kind: "device"; deviceId: string; processing: boolean }
  | { kind: "file"; buffer: AudioBuffer; loop: boolean };

export const FILE_MAX_BYTES = 50 * 1024 * 1024;
export const FILE_MAX_S = 600;

/** TX audio input: the chosen source -> input gain -> the capture worklet, which delivers 20 ms PCM16 blocks
 * via `onChunk` (mic-capture.worklet.js). The caller decides what to send -- see App.svelte: only while PTT
 * is held and the TX mode takes audio. A file plays only between playFromStart() and stopPlayback(). */
export class TxAudioInput {
  private ctx: AudioContext | null = null;
  private stream: MediaStream | null = null;
  private node: AudioWorkletNode | null = null;
  private gain: GainNode | null = null;
  private source: MediaStreamAudioSourceNode | null = null;
  private player: AudioBufferSourceNode | null = null;
  private current: TxSource | null = null;
  private gainDb = 0;

  /** captureTime: the block's position on the AudioContext clock -- lets
   * the caller drop blocks recorded BEFORE PTT was pressed. */
  onChunk: (pcm16: Int16Array, sampleRateHz: number, captureTime: number) => void = () => {};
  /** The source stopped on its own: a file without loop reached its end, or the input device went away. */
  onSourceEnded: (why: "file_end" | "device_gone") => void = () => {};

  /** Opens `src` (a no-op if it is already the open source). */
  async start(src: TxSource): Promise<void> {
    if (this.ctx && this.current === src) return;
    this.stop();
    let ctx: AudioContext;
    try {
      // 48 kHz = the TX flowgraph's own rate, so the backend needs no
      // resampling (it still copes with other rates, see backend tx_audio.py).
      ctx = new AudioContext({ sampleRate: 48000 });
    } catch {
      ctx = new AudioContext();
    }
    this.ctx = ctx;
    this.current = src;
    try {
      await ctx.audioWorklet.addModule(micWorkletUrl);
      // mono for the worklet (it reads channel 0): a stereo file or device is mixed down, (L+R)/2
      this.gain = new GainNode(ctx, { channelCount: 1, channelCountMode: "explicit", channelInterpretation: "speakers" });
      this.gain.gain.value = Math.pow(10, this.gainDb / 20);
      this.node = new AudioWorkletNode(ctx, "mic-capture", { numberOfOutputs: 0 });
      this.node.port.onmessage = (e) => {
        const { pcm16, captureTime } = e.data as { pcm16: Int16Array; captureTime: number };
        this.onChunk(pcm16, ctx.sampleRate, captureTime);
      };
      this.gain.connect(this.node);
      if (src.kind === "device") {
        this.stream = await navigator.mediaDevices.getUserMedia({
          audio: {
            deviceId: src.deviceId ? { exact: src.deviceId } : undefined,
            channelCount: 1,
            echoCancellation: src.processing,
            noiseSuppression: src.processing,
            autoGainControl: src.processing,
          },
        });
        for (const t of this.stream.getAudioTracks()) t.onended = () => this.onSourceEnded("device_gone");
        this.source = ctx.createMediaStreamSource(this.stream);
        this.source.connect(this.gain);
      }
    } catch (err) {
      this.stop();
      throw err;
    }
  }

  /** File source: playback from the beginning (PTT pressed). */
  playFromStart(): void {
    if (!this.ctx || !this.gain || this.current?.kind !== "file") return;
    this.stopPlayback();
    const player = this.ctx.createBufferSource();
    player.buffer = this.current.buffer;
    player.loop = this.current.loop;
    player.onended = () => {
      if (this.player === player) {
        this.player = null;
        this.onSourceEnded("file_end");
      }
    };
    player.connect(this.gain);
    this.player = player;
    player.start();
  }

  /** File source: stop playback (PTT released). */
  stopPlayback(): void {
    const player = this.player;
    this.player = null;
    if (player) {
      player.onended = null;
      try {
        player.stop();
      } catch {
        /* never started */
      }
      player.disconnect();
    }
  }

  setGainDb(db: number): void {
    this.gainDb = db;
    if (this.gain) this.gain.gain.value = Math.pow(10, db / 20);
  }

  stop(): void {
    this.stopPlayback();
    this.source?.disconnect();
    this.gain?.disconnect();
    this.node?.disconnect();
    this.stream?.getTracks().forEach((t) => {
      t.onended = null;
      t.stop();
    });
    void this.ctx?.close();
    this.node = null;
    this.gain = null;
    this.source = null;
    this.stream = null;
    this.ctx = null;
    this.current = null;
  }

  get currentTime(): number {
    return this.ctx?.currentTime ?? 0;
  }

  get isActive(): boolean {
    return this.ctx !== null;
  }

  get isPlaying(): boolean {
    return this.player !== null;
  }
}

/** The browser's audio inputs. Their names only show after the page was allowed to record once
 * (grantAudioInputs). */
export async function listAudioInputs(): Promise<{ deviceId: string; label: string }[]> {
  if (!navigator.mediaDevices?.enumerateDevices) return [];
  const all = await navigator.mediaDevices.enumerateDevices();
  return all
    .filter((d) => d.kind === "audioinput")
    .map((d, i) => ({ deviceId: d.deviceId, label: d.label || `Eingang ${i + 1}` }));
}

/** Asks for recording permission once (the prompt), so that listAudioInputs() gets the device names. */
export async function grantAudioInputs(): Promise<void> {
  const s = await navigator.mediaDevices.getUserMedia({ audio: true });
  s.getTracks().forEach((t) => t.stop());
}

/** Decodes an audio file (WAV/MP3/OGG/... whatever the browser can) for the file source. */
export async function decodeAudioFile(file: File): Promise<AudioBuffer> {
  if (file.size > FILE_MAX_BYTES) throw new Error(`Datei zu groß (max. ${FILE_MAX_BYTES / 1024 / 1024} MB)`);
  const ctx = new OfflineAudioContext(1, 1, 48000);
  const buffer = await ctx.decodeAudioData(await file.arrayBuffer());
  if (buffer.duration > FILE_MAX_S) throw new Error(`Datei zu lang (max. ${FILE_MAX_S / 60} min)`);
  return buffer;
}
