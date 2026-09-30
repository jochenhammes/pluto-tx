/**
 * Client for the Web-TRX WebSocket protocol -- mirrors
 * backend/web_trx/protocol.py exactly (frame layouts, field order, byte
 * order). Text frames are JSON events/requests; binary frames start with a
 * 1-byte type tag.
 */

const FRAME_SPECTRUM = 0x01;
const FRAME_RX_AUDIO = 0x02;
const FRAME_TX_AUDIO = 0x03;

export interface SpectrumRow {
  generation: number;
  centerHz: number;
  spanHz: number;
  row: Float32Array;
}

export interface AudioChunk {
  sampleRateHz: number;
  pcm16: Int16Array;
}

export interface ServerEvent {
  event: string;
  [key: string]: unknown;
}

// Reconnect delays after a lost connection; the last one repeats.
const RECONNECT_DELAYS_MS = [500, 1000, 2000, 5000];

export class WebTrxClient {
  private ws: WebSocket | null = null;
  private url = "";
  private wanted = false; // false after close(): no more reconnects
  private attempt = 0;
  private timer: ReturnType<typeof setTimeout> | null = null;

  onEvent: (e: ServerEvent) => void = () => {};
  onSpectrum: (s: SpectrumRow) => void = () => {};
  onAudio: (a: AudioChunk) => void = () => {};
  onOpen: () => void = () => {};
  /** wasOpen: false if the attempt never got through (server down, or the
   * handshake was refused because the session is no longer valid). code:
   * the WebSocket close code (4000: the server closed us as unresponsive). */
  onClose: (wasOpen: boolean, code: number) => void = () => {};

  /** Connects and keeps the connection up: after a drop it reconnects with
   * backoff until close() is called. Each new connection starts with a
   * fresh 'hello' carrying the full server state. */
  connect(url: string): void {
    this.url = url;
    this.wanted = true;
    this.attempt = 0;
    this.open();
  }

  private open(): void {
    const ws = new WebSocket(this.url);
    let wasOpen = false;
    ws.binaryType = "arraybuffer";
    ws.onopen = () => {
      wasOpen = true;
      this.attempt = 0;
      this.onOpen();
    };
    ws.onclose = (ev: CloseEvent) => {
      if (this.ws === ws) this.ws = null;
      this.onClose(wasOpen, ev.code);
      if (!this.wanted) return;
      const delay = RECONNECT_DELAYS_MS[Math.min(this.attempt++, RECONNECT_DELAYS_MS.length - 1)];
      this.timer = setTimeout(() => this.open(), delay);
    };
    ws.onmessage = (ev: MessageEvent) => {
      if (typeof ev.data === "string") {
        const e = JSON.parse(ev.data) as ServerEvent;
        if (e.event === "hb") {
          // Server heartbeat (backend session.py): answered right here from the event loop -- the proof that
          // this page is alive. No answer for 5 s ends any transmission on the server.
          if (ws.readyState === WebSocket.OPEN) ws.send(JSON.stringify({ request: "hb_ack", seq: e.seq }));
          return;
        }
        this.onEvent(e);
      } else {
        this.handleBinary(ev.data as ArrayBuffer);
      }
    };
    this.ws = ws;
  }

  private handleBinary(buf: ArrayBuffer): void {
    const view = new DataView(buf);
    const type = view.getUint8(0);
    if (type === FRAME_SPECTRUM) {
      // Matches protocol._SPECTRUM_HEADER = struct.Struct("<BIdd"): 1 + 4 + 8 + 8 = 21 bytes.
      const generation = view.getUint32(1, true);
      const centerHz = view.getFloat64(5, true);
      const spanHz = view.getFloat64(13, true);
      const row = new Float32Array(buf.slice(21));
      this.onSpectrum({ generation, centerHz, spanHz, row });
    } else if (type === FRAME_RX_AUDIO) {
      // Matches protocol._AUDIO_HEADER = struct.Struct("<BI"): 1 + 4 = 5 bytes.
      const sampleRateHz = view.getUint32(1, true);
      const pcm16 = new Int16Array(buf.slice(5));
      this.onAudio({ sampleRateHz, pcm16 });
    }
  }

  get isOpen(): boolean {
    return this.ws?.readyState === WebSocket.OPEN;
  }

  request(name: string, params: Record<string, unknown> = {}): void {
    if (!this.isOpen) return;
    this.ws?.send(JSON.stringify({ request: name, ...params }));
  }

  sendTxAudio(sampleRateHz: number, pcm16: Int16Array): void {
    const frame = new Uint8Array(5 + pcm16.byteLength);
    const header = new DataView(frame.buffer);
    header.setUint8(0, FRAME_TX_AUDIO);
    header.setUint32(1, sampleRateHz, true);
    frame.set(new Uint8Array(pcm16.buffer, pcm16.byteOffset, pcm16.byteLength), 5);
    if (this.isOpen) this.ws?.send(frame);
  }

  close(): void {
    this.wanted = false;
    if (this.timer) clearTimeout(this.timer);
    this.timer = null;
    this.ws?.close();
    this.ws = null;
  }
}
