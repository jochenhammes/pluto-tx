<script lang="ts">
  import { onMount } from "svelte";
  import Waterfall from "./lib/Waterfall.svelte";
  import FreqInput from "./lib/FreqInput.svelte";
  import RxDecoder, {
    type PocsagCall, type RadeStatus, type Ft8Slot, type Ft8Status, type Ft8Decode,
  } from "./lib/RxDecoder.svelte";
  import Login from "./lib/Login.svelte";
  import { WebTrxClient, type ServerEvent, type SpectrumRow, type AudioChunk } from "./lib/ws";
  import { AudioPlayer, MicCapture, type PlayerStats } from "./lib/audio";
  import { checkSession, checkSessionOrNull, logout as apiLogout, fetchTxLog, type TxLogEntry } from "./lib/auth";

  // Filled from the backend's "hello" event (SessionBackend.device_types()):
  // "sim" for SimBackend, pluto/hackrf/rtlsdr for GnuRadioBackend.
  let rxDeviceTypes: [string, string][] = [];
  let txDeviceTypes: [string, string][] = [];
  const MODE_LABELS: Record<string, string> = {
    fm: "FM", ssb: "SSB (USB)", lsb: "SSB (LSB)", m17: "M17", rade: "RADE",
    pocsag: "POCSAG", rtty: "RTTY", digitext: "Waterfall Writer", ft8: "FT8",
  };
  // Which modes exist comes from the backend (features.rx_modes/tx_modes:
  // e.g. RADE only where librade is installed).
  const DEFAULT_MODES = ["fm", "ssb", "lsb", "m17", "pocsag"];
  $: rxModes = (features.rx_modes ?? DEFAULT_MODES).map((m) => [m, MODE_LABELS[m] ?? m] as [string, string]);
  $: txModes = (features.tx_modes ?? DEFAULT_MODES).map((m) => [m, MODE_LABELS[m] ?? m] as [string, string]);
  // One-shot text modes: a single "send" click, the server renders the
  // whole transmission and unkeys on its own. Everything else takes the mic.
  // FT8: the click arms a series of slot-timed transmissions instead.
  const ONE_SHOT_MODES = ["pocsag", "rtty", "digitext", "ft8"];
  const isAudioMode = (mode: string) => !ONE_SHOT_MODES.includes(mode);

  let waterfall: Waterfall;
  const client = new WebTrxClient();
  const audioPlayer = new AudioPlayer();
  const mic = new MicCapture();

  let authChecked = false;
  let authenticated = false;
  let wsConnected = false;
  let backendName = "";
  let events: string[] = [];
  let txLog: TxLogEntry[] = [];

  // -- RX state --
  let rxDeviceType = "";
  let rxScanned: [string, string][] = [];
  let rxConnection = "";
  let rxConnected = false;
  let rxMode = "fm";
  let rxFreqHz = 432_500_000;
  let floorDb = -100;
  let ceilingDb = -20;
  let autoLevel = true;
  // Waterfall zoom/averaging/FFT size. With a backend that zooms itself
  // (features.fft_zoom_max, GnuRadioBackend) zoom is a real zoom-FFT on the
  // server; otherwise (SimBackend) the waterfall crops rows client-side.
  const ZOOM_STEPS = [1, 2, 3, 4, 6, 8, 12, 16, 24, 32, 48, 64, 96, 128];
  let zoomIndex = 0;
  $: zoom = ZOOM_STEPS[zoomIndex] ?? 1;
  let features: {
    fft_zoom_max?: number; fft_avg_max?: number; fft_sizes?: number[]; rx_modes?: string[]; tx_modes?: string[];
    rx_sample_rates?: Record<string, number[]>;
    rx_gain?: Record<string, RxGainInfo>;
    rx_direct_sampling?: string[];
    rx_bands?: Record<string, [number, number]>;
    ft8?: { tx: boolean; rx_backends: string[]; clock_synced: boolean };
  } = {};
  $: serverZoom = !!features.fft_zoom_max;
  $: zoomSteps = ZOOM_STEPS.filter((z) => z <= (features.fft_zoom_max ?? 8));
  let fftAvg = 1;
  // RX bandwidth (device sample rate, "" = device default) and oscillator
  // corrections in ppm, see radio_backend.PPM_RANGE.
  let rxSampleRate: number | "" = "";
  // RTL-SDR direct sampling (HF): off / i / q
  let rxDirectSampling = "off";
  // FT8 receive
  let rxFt8Decoder = "auto";
  let ft8Slots: Ft8Slot[] = [];
  let ft8Status: Ft8Status | null = null;
  let clockSynced = true;
  // Slot clock: the server's time decides; serverOffsetS = server - browser.
  let serverOffsetS = 0;
  let nowS = Date.now() / 1000;
  setInterval(() => (nowS = Date.now() / 1000 + serverOffsetS), 200);
  $: slotPos = ((nowS % 15) + 15) % 15;
  $: slotEven = Math.floor(nowS / 15) % 2 === 0;
  $: utcNow = new Date(nowS * 1000).toISOString().slice(11, 19);
  let rxPpm = 0;
  let txPpm = 0;
  $: rxSampleRates = features.rx_sample_rates?.[rxDeviceType] ?? [];
  let fftSize = 2048;
  let wfAreaHeight = 420;
  let audioOn = false;
  let rxError = "";
  // Bottom area: one panel with tabs; its height is set by dragging the
  // divider above it (the waterfall takes the rest), kept per browser.
  type BottomTab = "rx" | "txlog" | "events";
  let bottomTab: BottomTab = "rx";
  const BOTTOM_DEFAULT_PX = 220;
  const BOTTOM_MIN_PX = 110;
  let bottomHeight = BOTTOM_DEFAULT_PX;
  try {
    bottomHeight = Number(localStorage.getItem("webtrx.bottomHeight")) || BOTTOM_DEFAULT_PX;
  } catch {
    /* storage unavailable: default height */
  }
  function clampBottom(px: number): number {
    // Keep at least ~260 px for header, RX controls and a usable waterfall.
    return Math.round(Math.max(BOTTOM_MIN_PX, Math.min(px, window.innerHeight - 260)));
  }
  function startResize(e: PointerEvent): void {
    const handle = e.currentTarget as HTMLElement;
    const startY = e.clientY;
    const startHeight = bottomHeight;
    handle.setPointerCapture(e.pointerId);
    const move = (ev: PointerEvent) => (bottomHeight = clampBottom(startHeight - (ev.clientY - startY)));
    const end = () => {
      handle.removeEventListener("pointermove", move);
      handle.removeEventListener("pointerup", end);
      handle.removeEventListener("pointercancel", end);
      try {
        localStorage.setItem("webtrx.bottomHeight", String(bottomHeight));
      } catch {
        /* ignore */
      }
    };
    handle.addEventListener("pointermove", move);
    handle.addEventListener("pointerup", end);
    handle.addEventListener("pointercancel", end);
  }
  function resetBottomHeight(): void {
    bottomHeight = BOTTOM_DEFAULT_PX;
    try {
      localStorage.removeItem("webtrx.bottomHeight");
    } catch {
      /* ignore */
    }
  }
  let rxAudioStats: PlayerStats | null = null;
  // RX volume: purely local to this browser (a gain node in the player),
  // remembered in localStorage.
  let volumePct = 100;
  try {
    volumePct = Number(localStorage.getItem("webtrx.volumePct") ?? 100) || 0;
  } catch {
    /* storage unavailable: default volume */
  }
  $: {
    audioPlayer.setVolume(volumePct / 100);
    try {
      localStorage.setItem("webtrx.volumePct", String(volumePct));
    } catch {
      /* ignore */
    }
  }
  // RX gain controls, built from what the device declares (features.rx_gain):
  // AGC mode (if any) plus one control per gain stage.
  type GainStage = {
    name: string; label: string; kind: "continuous_db" | "bool"; min: number; max: number;
    step: number; unit: string; default: number | boolean; controls_agc: boolean;
  };
  type RxGainInfo = { agc_modes: string[]; default_gain_mode: string | null; stages: GainStage[] };
  const AGC_LABELS: Record<string, string> = {
    manual: "Manuell", agc: "AGC", slow_attack: "AGC langsam", fast_attack: "AGC schnell", hybrid: "AGC hybrid",
  };
  // Squelch threshold (server side, gates the browser audio) and the channel
  // level it compares against (rx_level events). At or below SQUELCH_OFF: off.
  const SQUELCH_OFF = -120;
  let squelchDb = SQUELCH_OFF;
  let rxLevelDb: number | null = null;
  let squelchOpen = true;
  const LEVEL_MIN = -120;
  const LEVEL_MAX = 0;
  const levelPct = (db: number) => Math.max(0, Math.min(100, ((db - LEVEL_MIN) / (LEVEL_MAX - LEVEL_MIN)) * 100));
  let rxGainMode = "";
  let rxStageValues: Record<string, number | boolean> = {};
  $: rxGainInfo = features.rx_gain?.[rxDeviceType] ?? null;
  $: if (rxGainInfo) {
    if (!rxGainInfo.agc_modes.includes(rxGainMode)) rxGainMode = rxGainInfo.default_gain_mode ?? "";
    for (const st of rxGainInfo.stages) if (!(st.name in rxStageValues)) rxStageValues[st.name] = st.default;
  }
  function setRxGain(name: string, value: number | boolean | string): void {
    client.request("set_gain", { direction: "rx", name, value: typeof value === "boolean" ? Number(value) : value });
  }
  audioPlayer.onStats = (st) => (rxAudioStats = st);
  let rxDeemphasis = true;

  // Choice lists come from the backend's 'hello' (web_trx/modes.py), which
  // in turn is test-checked against pluto-tx's config -- no third
  // hardcoded copy of the CTCSS table or deviation choices here.
  interface FmOptions {
    deviation_choices_hz: number[];
    deviation_default_hz: number;
    preemphasis_default: boolean;
    deemphasis_default: boolean;
    ctcss_tones_hz: number[];
  }
  let fmOptions: FmOptions | null = null;
  interface RttyOptions {
    mark_hz_default: number; mark_hz_range: [number, number]; shift_hz_default: number;
    shift_hz_choices: number[]; baud_default: number; baud_choices: number[]; max_text_len: number;
  }
  interface DigitextOptions {
    layouts: string[]; zoom_range: [number, number]; min_freq_hz_range: [number, number];
    min_freq_hz_default: number; max_text_len: number;
  }
  let rttyOptions: RttyOptions | null = null;
  let digitextOptions: DigitextOptions | null = null;
  // RTTY settings, separately for RX (decoder) and TX
  let rxRtty = { mark_hz: 2125, shift_hz: 170, baud: 45.45, reverse: false };
  let txRtty = { mark_hz: 2125, shift_hz: 170, baud: 45.45, reverse: false };
  let rttyTxText = "CQ CQ DE DA2JH";
  let digitext = { text: "DA2JH", layout: "horizontal", zoom: 1, min_freq_hz: 4000 };
  let radeEoo = false;
  // FT8 transmit (backend web_trx/modes.py, ft8_series.py): manual QSO -- the
  // operator picks the message per series; the server builds the text from
  // the station data (preview in ft8TxText) and times it to the UTC slots.
  interface Ft8TxOptions {
    message_kinds: string[]; free_text_max: number; report_range_db: [number, number];
    tone_range_hz: [number, number]; tone_default_hz: number; slots: string[]; max_repeats: number;
  }
  const FT8_KIND_LABELS: Record<string, string> = {
    cq: "CQ", reply: "Antwort (Call + Locator)", report: "Rapport", r_report: "R + Rapport",
    rrr: "RRR", rr73: "RR73", "73": "73", free: "Freitext",
  };
  const FT8_SLOT_LABELS: Record<string, string> = { any: "nächster", even: "1. (:00/:30)", odd: "2. (:15/:45)" };
  let ft8Options: Ft8TxOptions | null = null;
  let ft8Tx = {
    kind: "cq", dx_call: "", report_db: -10, free_text: "", offset_hz: 1500, slot: "any",
    drift_comp: true, repeat_count: 1,
  };
  let ft8TxText = "";
  let ft8Armed: { start_at: number; slot_utc: string; parity: string; repetition: number; of: number } | null = null;
  $: ft8CountdownS = ft8Armed ? Math.max(0, Math.ceil(ft8Armed.start_at - nowS)) : 0;
  // What the receiver decoded (RxDecoder panel)
  let rttyRxText = "";
  let pocsagCalls: PocsagCall[] = [];
  let m17Caller: { src: string; dst: string; time: string } | null = null;
  let radeStatus: RadeStatus | null = null;

  // -- TX state --
  let txDeviceType = "";
  let txScanned: [string, string][] = [];
  let txConnection = "";
  let txConnected = false;
  let txMode = "fm";
  let txFreqHz = 432_500_000;
  let ctcssHz: number | "" = "";
  let fmDeviationHz = 2500;
  let fmPreemphasis = true;
  let srcCallsign = "";
  // Station data (backend web_trx/station.py): used by FT8 and as the M17
  // source callsign when that is still empty.
  let stationCall = "";
  let stationLocator = "";
  let stationSaved = { call: "", locator: "" }; // the server's value, restored after a refused edit
  function applyStation(st: { call?: string; locator?: string } | undefined): void {
    stationSaved = { call: st?.call ?? "", locator: st?.locator ?? "" };
    stationCall = stationSaved.call;
    stationLocator = stationSaved.locator;
    if (!srcCallsign && stationCall) srcCallsign = stationCall;
  }
  function saveStation(): void {
    client.request("set_station", { call: stationCall, locator: stationLocator });
  }
  let dstCallsign = "@ALL";
  let ric = 1234567;
  let pocsagText = "DE DA2JH";
  let keyed = false;
  let micError = "";
  let pttHeld = false; // voice modes: mic audio is only sent while held
  let pttRequestedAt = 0;
  let keyLatencyMs: number | null = null;
  let txAudioStats: {
    buffer_ms: number; underruns: number; dropped_ms: number;
    audio_s: number; wall_s: number; max_gap_ms: number; final?: boolean;
  } | null = null;
  type TxPower = {
    label: string; unit: string; min: number; ceiling: number; value: number;
    max?: number; default_ceiling?: number;
    // further switchable stages, e.g. the HackRF's RF amp (+14 dB); off on every new connect
    secondary?: Array<{ name: string; label: string; kind: string; min: number; max: number; unit: string;
      value: number | boolean }>;
  };
  let txPower: TxPower | null = null;
  // FM/SSB audio processing (backend TX_SETTINGS in radio_backend.py);
  // null with a backend that has none (SimBackend).
  type TxSettings = {
    nf_gain: number; gate_enabled: boolean; gate_threshold_db: number;
    compressor_enabled: boolean; compressor_threshold_db: number; compressor_ratio: number;
    limiter_enabled: boolean; subtone_level_pct: number;
  };
  let txSettings: TxSettings | null = null;
  let micLevelDb = -90;
  let micMonitor = false; // mic open without keying, to set levels
  let compressorGrDb: number | null = null;
  // After E-STOP the TX side stays dark until the operator re-arms it.
  let txNeedsRearm = false;
  let txError = "";
  // What the server last got via select_mode for TX -- PTT sends pending
  // field edits first (a field only fires "change" on blur, and the PTT
  // button deliberately doesn't take focus).
  let lastTxModeRequest = "";
  let pttStartCtxTime = 0;
  // Set when THIS client asked to connect: after "connected" it starts the
  // flowgraph with the selected mode (the real backend only builds it on
  // select_mode; without it the waterfall stays empty).
  const pendingModeSelect = { rx: false, tx: false };

  function log(line: string): void {
    events = [line, ...events].slice(0, 80);
  }

  // -- messages shown as banners below the header (errors, connection state) --
  type Notice = { id: number; text: string; kind: "error" | "info" };
  let notices: Notice[] = [];
  let noticeSeq = 0;
  function pushNotice(text: string, kind: Notice["kind"] = "error"): void {
    const id = ++noticeSeq;
    notices = [...notices.filter((n) => n.text !== text), { id, text, kind }].slice(-3);
    setTimeout(() => dismissNotice(id), kind === "error" ? 8000 : 3000);
  }
  function dismissNotice(id: number): void {
    notices = notices.filter((n) => n.id !== id);
  }

  let wsEverConnected = false;
  let wsLostNotified = false;
  client.onOpen = () => {
    wsConnected = true;
    if (wsEverConnected) pushNotice("Verbindung zum Server wiederhergestellt", "info");
    wsEverConnected = true;
    wsLostNotified = false;
    log("WS verbunden");
  };
  client.onClose = (wasOpen) => {
    wsConnected = false;
    // The server's watchdog unkeys a voice over without mic audio anyway;
    // locally just stop sending and wait for the fresh 'hello'.
    pttHeld = false;
    if (wasOpen) log("WS getrennt");
    if (!wsLostNotified && wsEverConnected) {
      pushNotice("Verbindung zum Server verloren — verbinde neu…");
      wsLostNotified = true;
    }
    if (!wasOpen) {
      // Refused handshake = session gone (e.g. expired) -> back to login.
      void checkSessionOrNull().then((ok) => {
        if (ok === false && authenticated) {
          client.close();
          authenticated = false;
          pushNotice("Sitzung abgelaufen — bitte neu anmelden");
        }
      });
    }
  };
  type DirSnapshot = { device_type: string | null; connection: string | null; mode: string | null;
    freq_hz: number; mode_params: Record<string, unknown>; power?: TxPower | null; settings?: TxSettings;
    gains?: Record<string, number>; ft8_armed?: boolean };

  // Take over the server's actual state -- another tab or a script may have
  // changed it, and the server (not this page) is what actually transmits.
  function applySnapshot(rx: DirSnapshot, tx: DirSnapshot): void {
    // "" is a valid connection (auto-detect, e.g. RTL-SDR/HackRF); only null means disconnected.
    rxConnected = rx.connection !== null && rx.connection !== undefined;
    if (rx.device_type) rxDeviceType = rx.device_type;
    if (rx.mode) rxMode = rx.mode;
    rxFreqHz = rx.freq_hz;
    const g = rx.gains || {};
    if (g.fft_zoom) zoomIndex = Math.max(0, ZOOM_STEPS.indexOf(g.fft_zoom));
    if (g.fft_avg) fftAvg = g.fft_avg;
    if (g.fft_size) fftSize = g.fft_size;
    rxSampleRate = g.sample_rate ?? "";
    rxDirectSampling = (rx.gains as Record<string, unknown> | undefined)?.direct_sampling as string ?? "off";
    rxPpm = g.freq_correction_ppm ?? 0;
    const rawGains = (rx.gains ?? {}) as Record<string, unknown>;
    squelchDb = typeof rawGains.squelch_db === "number" ? rawGains.squelch_db : SQUELCH_OFF;
    if (typeof rawGains.gain_mode === "string") rxGainMode = rawGains.gain_mode;
    rxStageValues = {};
    for (const [k, v] of Object.entries(rawGains)) if (k.startsWith("stage:")) rxStageValues[k.slice(6)] = v as number | boolean;
    txPpm = tx.gains?.freq_correction_ppm ?? 0;
    txConnected = tx.connection !== null && tx.connection !== undefined;
    if (tx.device_type) txDeviceType = tx.device_type;
    if (tx.mode) txMode = tx.mode;
    txFreqHz = tx.freq_hz;
    const p = tx.mode_params || {};
    if (tx.mode === "pocsag") {
      if (p.ric !== undefined) ric = Number(p.ric);
      if (p.text !== undefined) pocsagText = String(p.text);
    }
    if (tx.mode === "m17") {
      if (p.src_callsign !== undefined) srcCallsign = String(p.src_callsign);
      if (p.dst_callsign !== undefined) dstCallsign = String(p.dst_callsign);
    }
    if (tx.mode === "fm") {
      ctcssHz = typeof p.ctcss_hz === "number" ? p.ctcss_hz : "";
      if (typeof p.deviation_hz === "number") fmDeviationHz = p.deviation_hz;
      if (typeof p.preemphasis === "boolean") fmPreemphasis = p.preemphasis;
    }
    if (rx.mode === "fm" && typeof rx.mode_params?.deemphasis === "boolean") rxDeemphasis = rx.mode_params.deemphasis;
    if (rx.mode === "rtty") rxRtty = { ...rxRtty, ...(rx.mode_params as typeof rxRtty) };
    if (rx.mode === "ft8" && typeof rx.mode_params?.decoder === "string") rxFt8Decoder = rx.mode_params.decoder;
    if (tx.mode === "rtty") {
      const { text, ...rest } = p as typeof txRtty & { text?: string };
      txRtty = { ...txRtty, ...rest };
      if (typeof text === "string") rttyTxText = text;
    }
    if (tx.mode === "digitext") digitext = { ...digitext, ...(p as typeof digitext) };
    if (tx.mode === "rade" && typeof p.eoo === "boolean") radeEoo = p.eoo;
    if (tx.mode === "ft8") ft8Tx = { ...ft8Tx, ...(p as Partial<typeof ft8Tx>) };
    // An armed series survives a page reload; its details come with the next ft8_armed.
    ft8Armed = tx.ft8_armed ? (ft8Armed ?? { start_at: 0, slot_utc: "…", parity: "", repetition: 0, of: 0 }) : null;
    txPower = tx.power ?? null;
    txSettings = tx.settings ?? null;
    txNeedsRearm = txConnected && !tx.mode;
    if (txConnected && tx.mode) lastTxModeRequest = JSON.stringify([txMode, txModeParams()]);
  }

  client.onEvent = (e: ServerEvent) => {
    if (e.event === "hello") {
      backendName = String(e.backend);
      features = (e.features as typeof features) ?? {};
      if (typeof e.server_time === "number") serverOffsetS = e.server_time - Date.now() / 1000;
      clockSynced = features.ft8?.clock_synced ?? true;
      if (fmOptions === null) {
        fmOptions = (e.mode_options as { fm: FmOptions }).fm;
        fmDeviationHz = fmOptions.deviation_default_hz;
        fmPreemphasis = fmOptions.preemphasis_default;
        rxDeemphasis = fmOptions.deemphasis_default;
      }
      const opts = e.mode_options as { rtty?: RttyOptions; digitext?: DigitextOptions; ft8?: Ft8TxOptions };
      rttyOptions = opts.rtty ?? null;
      digitextOptions = opts.digitext ?? null;
      if (ft8Options === null && opts.ft8?.message_kinds) {
        ft8Options = opts.ft8;
        ft8Tx.offset_hz = ft8Options.tone_default_hz;
      }
      const types = e.device_types as { rx: [string, string][]; tx: [string, string][] };
      rxDeviceTypes = types.rx;
      txDeviceTypes = types.tx;
      if (!rxDeviceTypes.some(([v]) => v === rxDeviceType)) rxDeviceType = rxDeviceTypes[0]?.[0] ?? "";
      if (!txDeviceTypes.some(([v]) => v === txDeviceType)) txDeviceType = txDeviceTypes[0]?.[0] ?? "";
      applySnapshot(e.rx as DirSnapshot, e.tx as DirSnapshot);
      applyStation(e.station as { call?: string; locator?: string });
    }
    if (e.event === "connected") {
      if (e.direction === "rx") rxConnected = true;
      if (e.direction === "tx") txConnected = true;
      const dir = e.direction as "rx" | "tx";
      if (pendingModeSelect[dir]) {
        pendingModeSelect[dir] = false;
        if (dir === "rx") selectRxMode();
        else selectTxMode();
      }
    }
    if (e.event === "tuned") {
      if (e.direction === "rx") rxFreqHz = Number(e.freq_hz);
      if (e.direction === "tx") txFreqHz = Number(e.freq_hz);
    }
    if (e.event === "mode") {
      if (e.direction === "rx") rxMode = String(e.mode);
      if (e.direction === "tx") txMode = String(e.mode);
      if (e.direction === "tx" && e.mode === "ft8") ft8TxText = String(e.text ?? "");
    }
    if (e.event === "ft8_armed") ft8Armed = e as unknown as typeof ft8Armed;
    if (e.event === "ft8_done") ft8Armed = null;
    if (e.event === "ft8_cancelled") {
      ft8Armed = null;
      if (e.reason === "error") txError = `FT8: ${String(e.message ?? "")}`;
      else if (e.reason !== "ptt_off") pushNotice(`FT8-Serie abgebrochen (${String(e.reason)})`, "info");
    }
    if (e.event === "tx_settings") txSettings = e.settings as TxSettings;
    if (e.event === "station") applyStation(e as { call?: string; locator?: string });
    if (e.event === "rtty_text") rttyRxText = (rttyRxText + String(e.text)).slice(-20000);
    if (e.event === "pocsag_message" && e.direction === "rx") {
      pocsagCalls = [...pocsagCalls, {
        time: new Date().toLocaleTimeString(), ric: Number(e.ric), func: Number(e.function),
        text: String(e.text ?? ""), baud: Number(e.baud),
      }].slice(-100);
    }
    if (e.event === "m17_fields" && e.src) {
      m17Caller = { src: String(e.src), dst: String(e.dst ?? ""), time: new Date().toLocaleTimeString() };
    }
    if (e.event === "rade_status") radeStatus = e as unknown as RadeStatus;
    if (e.event === "ft8_slot") {
      const slot = e as unknown as Ft8Slot;
      // One entry per slot (also keys the table): a repeated slot replaces the old one.
      ft8Slots = [...ft8Slots.filter((s) => s.slot_start !== slot.slot_start), slot].slice(-40);
    }
    if (e.event === "ft8_status") {
      ft8Status = e as unknown as Ft8Status;
      if (typeof ft8Status.clock_synced === "boolean") clockSynced = ft8Status.clock_synced;
    }
    if (e.event === "rx_level") {
      rxLevelDb = Number(e.level_db);
      squelchOpen = Boolean(e.squelch_open);
      return; // 5x per second -- not worth an event-log line
    }
    if (e.event === "mode" && e.direction === "rx") radeStatus = null;
    if (e.event === "disconnected") {
      if (e.direction === "rx") rxConnected = false;
      if (e.direction === "tx") {
        txConnected = false;
        ft8Armed = null;
      }
    }
    if (e.event === "scanned") {
      const devices = Object.entries(e.devices as Record<string, string>);
      if (e.direction === "rx") rxScanned = devices;
      if (e.direction === "tx") txScanned = devices;
    }
    if (e.event === "tx_power") txPower = e as unknown as TxPower;
    if (e.event === "tx_audio") {
      txAudioStats = e as unknown as typeof txAudioStats;
      compressorGrDb = (e.compressor_gr_db as number | null) ?? null;
    }
    if (e.event === "error") {
      pushNotice(String(e.message));
      if (e.request === "set_station") {
        stationCall = stationSaved.call;
        stationLocator = stationSaved.locator;
      }
      if (e.direction === "rx") rxError = String(e.message);
    }
    if (e.event === "connected" && e.direction === "rx") rxError = "";
    if (e.event === "mode" && e.direction === "rx") rxError = "";
    if (e.event === "error" && e.direction !== "rx" && (e.request === "ptt_on" || e.request === "select_mode" || e.request === "set_gain")) {
      txError = String(e.message);
      if (e.request === "ptt_on") pttHeld = false;
    }
    if (e.event === "keyed") {
      keyed = true;
      txError = "";
      if (pttRequestedAt) keyLatencyMs = Math.round(performance.now() - pttRequestedAt);
      pttRequestedAt = 0;
    }
    if (e.event === "unkeyed") {
      keyed = false;
      void refreshTxLog();
    }
    if (e.event === "estop") {
      keyed = false;
      ft8Armed = null;
      txNeedsRearm = true; // the backend drops the TX flowgraph on E-STOP
      void refreshTxLog();
    }
    if (e.event !== "tx_audio" || e.final) log(`${e.event} ${JSON.stringify(e)}`);
  };
  client.onSpectrum = (s: SpectrumRow) => waterfall?.enqueueRow(s.row, s.centerHz, s.spanHz);
  client.onAudio = (a: AudioChunk) => {
    if (audioOn) audioPlayer.push(a.pcm16, a.sampleRateHz);
  };

  function connectWs(): void {
    const proto = location.protocol === "https:" ? "wss" : "ws";
    client.connect(`${proto}://${location.host}/ws`);
  }

  async function refreshTxLog(): Promise<void> {
    txLog = await fetchTxLog(20);
  }

  async function handleLoginSuccess(): Promise<void> {
    authenticated = true;
    connectWs();
    await refreshTxLog();
  }

  async function doLogout(): Promise<void> {
    client.close();
    await apiLogout();
    location.reload(); // simplest full reset of all client-side session state
  }

  onMount(() => {
    void (async () => {
      authenticated = await checkSession();
      authChecked = true;
      if (authenticated) {
        connectWs();
        await refreshTxLog();
      }
    })();
    return () => client.close();
  });

  // -- RX actions --
  function scanRx(): void {
    client.request("scan", { direction: "rx", device_type: rxDeviceType });
  }
  function connectRx(): void {
    pendingModeSelect.rx = true;
    client.request("connect", { direction: "rx", device_type: rxDeviceType, connection: rxConnection });
  }
  function disconnectRx(): void {
    client.request("disconnect", { direction: "rx" });
  }
  function selectRxMode(): void {
    const params = rxMode === "fm" ? { deemphasis: rxDeemphasis } : rxMode === "rtty" ? rxRtty
      : rxMode === "ft8" ? { decoder: rxFt8Decoder } : {};
    client.request("select_mode", { direction: "rx", mode: rxMode, params });
  }
  function tuneRx(hz: number): void {
    rxFreqHz = hz;
    if (rxConnected) client.request("tune", { direction: "rx", freq_hz: hz });
  }
  function onWaterfallClick(freqHz: number): void {
    tuneRx(Math.round(freqHz / 100) * 100);
  }
  function toggleAudio(): void {
    audioOn = !audioOn;
    if (audioOn) audioPlayer.ensureStarted();
    else {
      audioPlayer.stop();
      rxAudioStats = null;
    }
  }

  // -- TX actions --
  function scanTx(): void {
    client.request("scan", { direction: "tx", device_type: txDeviceType });
  }
  function connectTx(): void {
    pendingModeSelect.tx = true;
    client.request("connect", { direction: "tx", device_type: txDeviceType, connection: txConnection });
  }
  function disconnectTx(): void {
    client.request("disconnect", { direction: "tx" });
  }
  function txModeParams(): Record<string, unknown> {
    if (txMode === "fm") {
      return { deviation_hz: fmDeviationHz, preemphasis: fmPreemphasis, ctcss_hz: ctcssHz === "" ? null : ctcssHz };
    }
    if (txMode === "m17") return { src_callsign: srcCallsign, dst_callsign: dstCallsign };
    if (txMode === "pocsag") return { ric, text: pocsagText };
    if (txMode === "rtty") return { ...txRtty, text: rttyTxText };
    if (txMode === "digitext") return { ...digitext };
    if (txMode === "rade") return { eoo: radeEoo };
    if (txMode === "ft8") return { ...ft8Tx, report_db: Math.round(Number(ft8Tx.report_db)),
                                   offset_hz: Number(ft8Tx.offset_hz), repeat_count: Math.round(Number(ft8Tx.repeat_count)) };
    return {};
  }
  function selectTxMode(): void {
    txNeedsRearm = false;
    txError = "";
    lastTxModeRequest = JSON.stringify([txMode, txModeParams()]);
    client.request("select_mode", { direction: "tx", mode: txMode, params: txModeParams() });
  }
  function tuneTx(hz: number): void {
    txFreqHz = hz;
    if (txConnected) client.request("tune", { direction: "tx", freq_hz: hz });
  }
  function setTxSetting(name: keyof TxSettings, value: number | boolean): void {
    client.request("set_gain", { direction: "tx", name, value: typeof value === "boolean" ? Number(value) : value });
  }
  const isVoiceMode = (mode: string) => mode === "fm" || mode === "ssb" || mode === "lsb";

  function commitPendingTxParams(): void {
    if (JSON.stringify([txMode, txModeParams()]) !== lastTxModeRequest) selectTxMode();
  }
  function setRxDisplay(name: string, value: number | string): void {
    client.request("set_gain", { direction: "rx", name, value });
  }
  function onZoomChange(): void {
    if (serverZoom) setRxDisplay("fft_zoom", zoom);
  }

  mic.onChunk = (pcm16, sr, captureTime) => {
    let peak = 0;
    for (let i = 0; i < pcm16.length; i++) peak = Math.max(peak, Math.abs(pcm16[i]));
    micLevelDb = peak > 0 ? 20 * Math.log10(peak / 32768) : -90;
    if (pttHeld && captureTime >= pttStartCtxTime) client.sendTxAudio(sr, pcm16);
  };
  async function toggleMicMonitor(): Promise<void> {
    if (micMonitor) {
      micMonitor = false;
      if (!pttHeld) mic.stop();
      micLevelDb = -90;
      return;
    }
    try {
      await mic.start();
      micMonitor = true;
      micError = "";
    } catch (err) {
      micError = `Mikrofon: ${(err as Error).message}`;
    }
  }
  function estop(): void {
    client.request("estop");
    pttHeld = false;
    micMonitor = false;
    if (mic.isActive) mic.stop();
  }
  function setTxPower(value: number): void {
    client.request("set_gain", { direction: "tx", name: "power", value });
  }

  // Audio TX modes (FM/SSB/LSB/M17): press-and-hold PTT, mic streamed for
  // the duration. POCSAG: a single click, the backend keys, sends the
  // message and auto-unkeys itself (see SimBackend.ptt()) -- no mic
  // involved, matching pluto-tx's own text-digimode PTT model.
  async function startAudioTx(): Promise<void> {
    if (!isAudioMode(txMode) || pttHeld || !txConnected) return;
    pttHeld = true;
    pttRequestedAt = performance.now();
    pttStartCtxTime = mic.isActive ? mic.currentTime - 0.05 : 0;
    commitPendingTxParams();
    client.request("ptt_on");
    try {
      // The mic stays open after the first PTT (permission prompt, device
      // start-up) -- chunks are only sent while pttHeld, see mic.onChunk.
      await mic.start();
      micError = "";
    } catch (err) {
      micError = `Mikrofon: ${(err as Error).message}`;
      log(`error mic: ${(err as Error).message}`);
      stopAudioTx();
    }
  }
  function stopAudioTx(): void {
    if (!pttHeld) return;
    pttHeld = false;
    client.request("ptt_off");
  }
  function isTyping(e: KeyboardEvent): boolean {
    const t = e.target as HTMLElement | null;
    return !!t && (t.tagName === "INPUT" || t.tagName === "TEXTAREA" || t.tagName === "SELECT");
  }
  // Space bar = PTT for voice modes (hold), except while typing in a field.
  function onKeyDown(e: KeyboardEvent): void {
    if (e.code !== "Space" || e.repeat || isTyping(e) || !isAudioMode(txMode)) return;
    e.preventDefault();
    void startAudioTx();
  }
  function onKeyUp(e: KeyboardEvent): void {
    if (e.code !== "Space" || !isAudioMode(txMode)) return;
    e.preventDefault();
    stopAudioTx();
  }
  function pocsagPtt(): void {
    commitPendingTxParams();
    client.request("ptt_on"); // one-shot; SimBackend/real POCSAG both auto-unkey, see docs/PROJECT_PLAN.md section 4
  }
  // FT8: first click arms the series (the server waits for the slot), a
  // second click cancels it -- in any state, also while transmitting.
  function ft8Ptt(): void {
    if (ft8Armed) {
      client.request("ptt_off");
      return;
    }
    commitPendingTxParams();
    client.request("ptt_on");
  }
  // Click on a received decode: its sender becomes the DX callsign, and the
  // reply goes into the other slot parity. The message kind stays the operator's.
  function pickFt8Decode(d: Ft8Decode, slot: Ft8Slot): void {
    if (!d.sender || ft8Armed) return;
    ft8Tx.dx_call = d.sender;
    ft8Tx.slot = Math.floor(slot.slot_start / 15) % 2 === 0 ? "odd" : "even";
    if (txMode === "ft8" && txConnected) selectTxMode();
  }

  function fmtFreq(hz: number | null): string {
    return hz === null ? "—" : `${Math.round(hz).toString().replace(/\B(?=(\d{3})+(?!\d))/g, ".")} Hz`;
  }
  function fmtTime(epochS: number): string {
    return new Date(epochS * 1000).toLocaleTimeString();
  }
  function fmtDuration(startedAt: number, endedAt: number | null): string {
    if (endedAt === null) return "läuft…";
    return `${(endedAt - startedAt).toFixed(1)} s`;
  }
</script>

<svelte:window on:keydown={onKeyDown} on:keyup={onKeyUp} on:blur={stopAudioTx} />

{#if !authChecked}
  <div class="loading">Web-TRX &mdash; lade&hellip;</div>
{:else if !authenticated}
  <Login onSuccess={handleLoginSuccess} />
{:else}
<div class="app">
<header class="topbar">
  <h1>Web-TRX</h1>
  <span class="status-pill" class:ok={wsConnected}>
    {wsConnected ? `verbunden · ${backendName}` : wsEverConnected ? "getrennt — verbinde neu…" : "verbinde…"}
  </span>
  <div class="spacer"></div>
  <div class="station" title="Eigenes Rufzeichen und Locator (für FT8, Vorbelegung M17)">
    <span class="dim">Station</span>
    <input class="st-call" type="text" placeholder="Rufzeichen" bind:value={stationCall} on:change={saveStation} />
    <input class="st-loc" type="text" placeholder="Locator" bind:value={stationLocator} on:change={saveStation} />
  </div>
  <button on:click={doLogout}>Abmelden</button>
  <button class="danger" on:click={estop}>NOTAUS</button>
</header>
{#if notices.length}
  <div class="notices">
    {#each notices as n (n.id)}
      <button class="notice {n.kind}" on:click={() => dismissNotice(n.id)} title="Klicken zum Schließen">{n.text}</button>
    {/each}
  </div>
{/if}

<div class="layout">
  <section class="panel rx-panel">
    <div class="panel-title">Empfang (RX)</div>

    <div class="row">
      <select bind:value={rxDeviceType} disabled={rxConnected}>
        {#each rxDeviceTypes as [v, l]}<option value={v}>{l}</option>{/each}
      </select>
      <button on:click={scanRx} disabled={rxConnected}>Scan</button>
      <select bind:value={rxConnection} disabled={rxConnected}>
        <option value="">(auto)</option>
        {#each rxScanned as [v, l]}<option value={v}>{l}</option>{/each}
      </select>
      {#if !rxConnected}
        <button class="primary" on:click={connectRx}>Verbinden</button>
      {:else}
        <button on:click={disconnectRx}>Trennen</button>
      {/if}
    </div>

    <div class="row rx-freq-row">
      <FreqInput value={rxFreqHz} onCommit={tuneRx} size="large" />
      {#if features.rx_direct_sampling?.includes(rxDeviceType)}
        <div class="field narrow">
          <label for="rxDirect" title="RTL-SDR ohne Tuner direkt abtasten: KW bis 28,8 MHz (über 14,4 MHz gespiegelt). RTL-SDR Blog V3: Q">Direct Sampling</label>
          <select id="rxDirect" bind:value={rxDirectSampling} disabled={!rxConnected}
            on:change={() => setRxDisplay("direct_sampling", rxDirectSampling)}>
            <option value="off">aus</option>
            <option value="i">I-Zweig</option>
            <option value="q">Q-Zweig (KW)</option>
          </select>
        </div>
      {/if}
      {#if rxSampleRates.length}
        <div class="field narrow">
          <label for="rxRate">Bandbreite</label>
          <select id="rxRate" bind:value={rxSampleRate} disabled={!rxConnected}
            on:change={() => rxSampleRate !== "" && setRxDisplay("sample_rate", rxSampleRate)}>
            <option value="">Standard</option>
            {#each rxSampleRates as r}<option value={r}>{(r / 1e6).toLocaleString("de-DE")} MS/s</option>{/each}
          </select>
        </div>
      {/if}
      <div class="field narrow">
        <label for="rxPpm" title="Oszillator-Korrektur des Empfängers (positiv = Gerät liegt zu hoch)">Korr. ppm</label>
        <input id="rxPpm" class="ppm" type="number" step="0.1" bind:value={rxPpm}
          on:change={() => setRxDisplay("freq_correction_ppm", Number(rxPpm))} />
      </div>
      <div class="field narrow">
        <label for="rxModeSel">Modus</label>
        <select id="rxModeSel" bind:value={rxMode} on:change={selectRxMode} disabled={!rxConnected}>
          {#each rxModes as [v, l]}<option value={v}>{l}</option>{/each}
        </select>
      </div>
      <div class="field narrow">
        <label for="rxAudioBtn">Audio</label>
        <button id="rxAudioBtn" on:click={toggleAudio}>{audioOn ? "\u{1F50A} RX-Audio an" : "\u{1F507} RX-Audio aus"}</button>
      </div>
      <div class="field narrow volume">
        <label for="rxVolume">Lautstärke {volumePct} %</label>
        <input id="rxVolume" type="range" min="0" max="200" step="5" bind:value={volumePct} />
      </div>
      {#if audioOn && rxAudioStats}
        <span class="dim" title="Jitter-Puffer der RX-Wiedergabe">Puffer {rxAudioStats.bufferedMs} ms · Aussetzer {rxAudioStats.underruns}</span>
      {/if}
      {#if rxMode === "fm"}
        <div class="field narrow">
          <span class="field-spacer" aria-hidden="true">&nbsp;</span>
          <label class="check">
            <input type="checkbox" bind:checked={rxDeemphasis} on:change={selectRxMode} disabled={!rxConnected} />
            De-Emphasis 750 &micro;s
          </label>
        </div>
      {/if}
    </div>
    {#if rxGainInfo}
      <div class="row rx-gain">
        {#if rxGainInfo.agc_modes.length}
          <div class="field narrow">
            <label for="rxGainMode">Verstärkung</label>
            <select id="rxGainMode" bind:value={rxGainMode} on:change={() => setRxGain("gain_mode", rxGainMode)}>
              {#each rxGainInfo.agc_modes as m}<option value={m}>{AGC_LABELS[m] ?? m}</option>{/each}
            </select>
          </div>
        {/if}
        <div class="field squelch">
          <label for="squelch">
            <span class="sq-dot" class:open={squelchOpen} title={squelchOpen ? "Squelch offen" : "Squelch zu"}></span>
            Squelch {squelchDb <= SQUELCH_OFF ? "aus" : `${squelchDb} dB`}
            {#if rxLevelDb !== null}<span class="dim">· Pegel {rxLevelDb.toFixed(0)} dB</span>{/if}
          </label>
          <div class="level" title="Kanalpegel, Strich = Squelch-Schwelle">
            <div class="level-fill" class:open={squelchOpen} style="width: {rxLevelDb === null ? 0 : levelPct(rxLevelDb)}%"></div>
            {#if squelchDb > SQUELCH_OFF}<div class="level-mark" style="left: {levelPct(squelchDb)}%"></div>{/if}
          </div>
          <input id="squelch" type="range" min={SQUELCH_OFF} max="0" step="1" bind:value={squelchDb}
            on:change={() => setRxGain("squelch_db", Number(squelchDb))} />
        </div>
        {#each rxGainInfo.stages as st (st.name)}
          {#if st.kind === "bool"}
            <label class="check"><input type="checkbox" checked={!!rxStageValues[st.name]}
              on:change={(e) => { rxStageValues[st.name] = e.currentTarget.checked; setRxGain(`stage:${st.name}`, e.currentTarget.checked); }} />
              {st.label}</label>
          {:else}
            {@const agcActive = st.controls_agc && rxGainMode !== "manual" && rxGainInfo.agc_modes.length > 0}
            <div class="field grow">
              <label for={`rxStage-${st.name}`}>{st.label} {agcActive ? "(AGC regelt)" : `${Number(rxStageValues[st.name]).toFixed(1)} ${st.unit}`}</label>
              <input id={`rxStage-${st.name}`} type="range" min={st.min} max={st.max} step={st.step || 0.5}
                value={Number(rxStageValues[st.name])} disabled={agcActive}
                on:change={(e) => { rxStageValues[st.name] = Number(e.currentTarget.value); setRxGain(`stage:${st.name}`, Number(e.currentTarget.value)); }} />
            </div>
          {/if}
        {/each}
      </div>
    {/if}
    {#if rxMode === "ft8"}
      <div class="row ft8-row">
        <div class="field narrow">
          <label for="rxFt8Dec">Decoder</label>
          <select id="rxFt8Dec" bind:value={rxFt8Decoder} on:change={selectRxMode}>
            <option value="auto">automatisch</option>
            {#each features.ft8?.rx_backends ?? [] as d}<option value={d}>{d}</option>{/each}
          </select>
        </div>
        <div class="slot-clock" title="15-s-Slots nach Server-Uhr">
          <div class="slot-label">{utcNow} UTC · {slotEven ? "1. Slot (:00/:30)" : "2. Slot (:15/:45)"}</div>
          <div class="slot-bar"><div class="slot-fill" class:odd={!slotEven} style="width: {(slotPos / 15) * 100}%"></div></div>
        </div>
        <span class="dim">USB, Decoder wertet jeden Slot nach ~14,8 s aus</span>
      </div>
      {#if !clockSynced}
        <div class="mic-error">Server-Uhr ist nicht per NTP synchronisiert — FT8 braucht ±1 s Genauigkeit.</div>
      {/if}
    {/if}
    {#if rxMode === "rtty" && rttyOptions}
      <div class="row">
        <div class="field">
          <label for="rxRttyMark">Mark Hz</label>
          <input id="rxRttyMark" type="number" min={rttyOptions.mark_hz_range[0]} max={rttyOptions.mark_hz_range[1]}
            step="5" bind:value={rxRtty.mark_hz} on:change={selectRxMode} />
        </div>
        <div class="field">
          <label for="rxRttyShift">Shift</label>
          <select id="rxRttyShift" bind:value={rxRtty.shift_hz} on:change={selectRxMode}>
            {#each rttyOptions.shift_hz_choices as v}<option value={v}>{v} Hz</option>{/each}
          </select>
        </div>
        <div class="field">
          <label for="rxRttyBaud">Baud</label>
          <select id="rxRttyBaud" bind:value={rxRtty.baud} on:change={selectRxMode}>
            {#each rttyOptions.baud_choices as v}<option value={v}>{v}</option>{/each}
          </select>
        </div>
        <label class="check"><input type="checkbox" bind:checked={rxRtty.reverse} on:change={selectRxMode} /> Reverse</label>
        <span class="dim">USB, Mark/Space-Töne im Audio · AFC läuft mit</span>
      </div>
    {/if}

    <div class="wf-area" bind:clientHeight={wfAreaHeight}>
    <Waterfall bind:this={waterfall} height={wfAreaHeight} {floorDb} {ceilingDb} zoom={serverZoom ? 1 : zoom} autoLevel={autoLevel && !keyed}
      markers={[
        ...(rxMode === "ft8" ? [{ hz: rxFreqHz + 200, widthHz: 2800, color: "rgba(62, 166, 255, 0.18)", label: "FT8" }] : []),
        // receive bandwidth of the current mode (features.rx_bands: [low, high] offsets from the RX frequency)
        ...(rxConnected && features.rx_bands?.[rxMode] ? [{
          hz: rxFreqHz + features.rx_bands[rxMode][0], widthHz: features.rx_bands[rxMode][1] - features.rx_bands[rxMode][0],
          color: "rgba(137, 147, 168, 0.12)", edgeColor: "rgba(170, 180, 200, 0.7)", label: "" }] : []),
        { hz: rxFreqHz, color: "#8993a8", label: "RX" },
        ...(txConnected ? [{ hz: txFreqHz, color: keyed ? "#ff4d4d" : "#f5c211", label: "TX" }] : []),
        // FT8: where our own signal sits (dial + offset, 8 tones = 50 Hz)
        ...(txConnected && txMode === "ft8" ? [{ hz: txFreqHz + Number(ft8Tx.offset_hz), widthHz: 50,
          color: keyed ? "rgba(255, 77, 77, 0.45)" : "rgba(245, 194, 17, 0.3)", label: "FT8 TX" }] : []),
      ]}
      onClickFreq={onWaterfallClick}
      onAutoLevel={(f, c) => { floorDb = f; ceilingDb = c; }} />
    </div>
    {#if rxError}<div class="mic-error">Empfänger: {rxError}</div>{/if}
    {#if !rxConnected}
      <div class="dim hint">Empfänger nicht verbunden — „Verbinden“ startet ihn im gewählten Modus.</div>
    {/if}

    <div class="row sliders wf-controls">
      <div class="field">
        <label for="floorDb">Floor {floorDb} dB</label>
        <input id="floorDb" type="range" min="-140" max="0" bind:value={floorDb} on:input={() => (autoLevel = false)} />
      </div>
      <div class="field">
        <label for="ceilingDb">Ceiling {ceilingDb} dB</label>
        <input id="ceilingDb" type="range" min="-100" max="40" bind:value={ceilingDb} on:input={() => (autoLevel = false)} />
      </div>
      <div class="field">
        <label for="zoomInput">Zoom ×{zoom}</label>
        <input id="zoomInput" type="range" min="0" max={zoomSteps.length - 1} step="1" bind:value={zoomIndex}
          on:change={onZoomChange} />
      </div>
      {#if features.fft_avg_max}
        <div class="field">
          <label for="fftAvg">Mittelung {fftAvg === 1 ? "aus" : `${fftAvg} Zeilen`}</label>
          <input id="fftAvg" type="range" min="1" max={features.fft_avg_max} step="1" bind:value={fftAvg}
            on:change={() => setRxDisplay("fft_avg", fftAvg)} />
        </div>
      {/if}
      {#if features.fft_sizes}
        <div class="field narrow">
          <label for="fftSize">FFT</label>
          <select id="fftSize" bind:value={fftSize} on:change={() => setRxDisplay("fft_size", fftSize)}>
            {#each features.fft_sizes as n}<option value={n}>{n}</option>{/each}
          </select>
        </div>
      {/if}
      <label class="check auto-level"><input type="checkbox" bind:checked={autoLevel} /> Auto-Pegel</label>
    </div>
  </section>

  <section class="panel tx-panel" class:keyed>
    <div class="panel-title">Senden (TX)</div>

    <div class="row">
      <select bind:value={txDeviceType} disabled={txConnected}>
        {#each txDeviceTypes as [v, l]}<option value={v}>{l}</option>{/each}
      </select>
      <button on:click={scanTx} disabled={txConnected}>Scan</button>
      <select bind:value={txConnection} disabled={txConnected}>
        <option value="">(auto)</option>
        {#each txScanned as [v, l]}<option value={v}>{l}</option>{/each}
      </select>
      {#if !txConnected}
        <button class="primary" on:click={connectTx}>Verbinden</button>
      {:else}
        <button on:click={disconnectTx}>Trennen</button>
      {/if}
    </div>

    <div class="row">
      <FreqInput value={txFreqHz} onCommit={tuneTx} size="large" disabled={keyed} />
      <button on:click={() => tuneTx(rxFreqHz)} disabled={keyed} title="RX-Frequenz übernehmen">= RX</button>
      <div class="field narrow">
        <label for="txPpm" title="Oszillator-Korrektur des Senders (positiv = Gerät liegt zu hoch)">Korr. ppm</label>
        <input id="txPpm" class="ppm" type="number" step="0.1" bind:value={txPpm} disabled={keyed}
          on:change={() => client.request("set_gain", { direction: "tx", name: "freq_correction_ppm", value: Number(txPpm) })} />
      </div>
    </div>

    <div class="row">
      <select id="txModeSel" bind:value={txMode} on:change={selectTxMode} disabled={!txConnected || keyed || !!ft8Armed}>
        {#each txModes as [v, l]}<option value={v}>{l}</option>{/each}
      </select>
      {#if txMode === "fm"}
        <div class="field">
          <label for="fmDeviation">Hub</label>
          <select id="fmDeviation" bind:value={fmDeviationHz} on:change={selectTxMode}>
            {#each fmOptions?.deviation_choices_hz ?? [] as d}
              <option value={d}>&plusmn;{(d / 1000).toFixed(1)} kHz {d <= 2500 ? "(schmal)" : "(breit)"}</option>
            {/each}
          </select>
        </div>
        <div class="field">
          <label for="ctcssHz">CTCSS</label>
          <select id="ctcssHz" bind:value={ctcssHz} on:change={selectTxMode}>
            <option value="">aus</option>
            {#each fmOptions?.ctcss_tones_hz ?? [] as t}
              <option value={t}>{t.toFixed(1)} Hz</option>
            {/each}
          </select>
        </div>
        <label class="check">
          <input type="checkbox" bind:checked={fmPreemphasis} on:change={selectTxMode} />
          Pre-Emphasis 750 &micro;s
        </label>
      {:else if txMode === "m17"}
        <div class="field">
          <label for="srcCallsign">Quell-Rufzeichen</label>
          <input id="srcCallsign" type="text" bind:value={srcCallsign} on:change={selectTxMode} />
        </div>
        <div class="field">
          <label for="dstCallsign">Ziel</label>
          <input id="dstCallsign" type="text" bind:value={dstCallsign} on:change={selectTxMode} />
        </div>
      {:else if txMode === "pocsag"}
        <div class="field">
          <label for="ric">RIC</label>
          <input id="ric" type="number" bind:value={ric} on:change={selectTxMode} />
        </div>
        <div class="field grow">
          <label for="pocsagText">Text</label>
          <input id="pocsagText" type="text" bind:value={pocsagText} on:change={selectTxMode} />
        </div>
      {:else if txMode === "rade"}
        <label class="check" title="End-of-Over-Kennung nach dem Loslassen (in pluto-tx noch nicht auf Hardware verifiziert)">
          <input type="checkbox" bind:checked={radeEoo} on:change={selectTxMode} /> End-of-Over senden
        </label>
      {/if}
    </div>
    {#if txMode === "rtty" && rttyOptions}
      <div class="row">
        <div class="field grow">
          <label for="rttyText">Text ({rttyTxText.length}/{rttyOptions.max_text_len})</label>
          <input id="rttyText" type="text" maxlength={rttyOptions.max_text_len} bind:value={rttyTxText} on:change={selectTxMode} />
        </div>
      </div>
      <div class="row">
        <div class="field">
          <label for="txRttyMark">Mark Hz</label>
          <input id="txRttyMark" type="number" min={rttyOptions.mark_hz_range[0]} max={rttyOptions.mark_hz_range[1]}
            step="5" bind:value={txRtty.mark_hz} on:change={selectTxMode} />
        </div>
        <div class="field">
          <label for="txRttyShift">Shift</label>
          <select id="txRttyShift" bind:value={txRtty.shift_hz} on:change={selectTxMode}>
            {#each rttyOptions.shift_hz_choices as v}<option value={v}>{v} Hz</option>{/each}
          </select>
        </div>
        <div class="field">
          <label for="txRttyBaud">Baud</label>
          <select id="txRttyBaud" bind:value={txRtty.baud} on:change={selectTxMode}>
            {#each rttyOptions.baud_choices as v}<option value={v}>{v}</option>{/each}
          </select>
        </div>
        <label class="check"><input type="checkbox" bind:checked={txRtty.reverse} on:change={selectTxMode} /> Reverse</label>
      </div>
    {:else if txMode === "digitext" && digitextOptions}
      <div class="row">
        <div class="field grow">
          <label for="dtText">Text ({digitext.text.length}/{digitextOptions.max_text_len})</label>
          <input id="dtText" type="text" maxlength={digitextOptions.max_text_len} bind:value={digitext.text} on:change={selectTxMode} />
        </div>
      </div>
      <div class="row">
        <div class="field">
          <label for="dtLayout">Anordnung</label>
          <select id="dtLayout" bind:value={digitext.layout} on:change={selectTxMode}>
            <option value="horizontal">horizontal</option>
            <option value="vertical">vertikal</option>
          </select>
        </div>
        <div class="field">
          <label for="dtZoom">Schriftgröße ×{digitext.zoom}</label>
          <input id="dtZoom" type="range" min={digitextOptions.zoom_range[0]} max={digitextOptions.zoom_range[1]} step="1"
            bind:value={digitext.zoom} on:change={selectTxMode} />
        </div>
        <div class="field">
          <label for="dtMinFreq">Abstand zum Träger {digitext.min_freq_hz} Hz</label>
          <input id="dtMinFreq" type="range" min={digitextOptions.min_freq_hz_range[0]} max={digitextOptions.min_freq_hz_range[1]}
            step="100" bind:value={digitext.min_freq_hz} on:change={selectTxMode} />
        </div>
      </div>
      <div class="dim">Erscheint im Wasserfall oberhalb der Sendefrequenz (USB + Abstand) — zum Mitlesen reinzoomen.</div>
    {:else if txMode === "ft8" && ft8Options}
      <fieldset class="ft8-tx" disabled={!!ft8Armed}>
        <div class="row">
          <div class="field">
            <label for="ft8Kind">Nachricht</label>
            <select id="ft8Kind" bind:value={ft8Tx.kind} on:change={selectTxMode}>
              {#each ft8Options.message_kinds as k}<option value={k}>{FT8_KIND_LABELS[k] ?? k}</option>{/each}
            </select>
          </div>
          {#if ft8Tx.kind === "free"}
            <div class="field grow">
              <label for="ft8Free">Freitext ({ft8Tx.free_text.length}/{ft8Options.free_text_max})</label>
              <input id="ft8Free" type="text" maxlength={ft8Options.free_text_max} bind:value={ft8Tx.free_text}
                on:change={selectTxMode} />
            </div>
          {:else if ft8Tx.kind !== "cq"}
            <div class="field">
              <label for="ft8Dx">DX-Rufzeichen</label>
              <input id="ft8Dx" type="text" class="callsign" bind:value={ft8Tx.dx_call} on:change={selectTxMode}
                placeholder="Klick auf Decode" />
            </div>
            {#if ft8Tx.kind === "report" || ft8Tx.kind === "r_report"}
              <div class="field narrow">
                <label for="ft8Report">Rapport dB</label>
                <input id="ft8Report" type="number" min={ft8Options.report_range_db[0]} max={ft8Options.report_range_db[1]}
                  step="1" bind:value={ft8Tx.report_db} on:change={selectTxMode} />
              </div>
            {/if}
          {/if}
        </div>
        <div class="row">
          <div class="field narrow">
            <label for="ft8Offset">Offset Hz</label>
            <input id="ft8Offset" type="number" min={ft8Options.tone_range_hz[0]} max={ft8Options.tone_range_hz[1]}
              step="10" bind:value={ft8Tx.offset_hz} on:change={selectTxMode} />
          </div>
          <div class="field">
            <label for="ft8Slot">Slot</label>
            <select id="ft8Slot" bind:value={ft8Tx.slot} on:change={selectTxMode}>
              {#each ft8Options.slots as s}<option value={s}>{FT8_SLOT_LABELS[s] ?? s}</option>{/each}
            </select>
          </div>
          <div class="field narrow">
            <label for="ft8Repeat">Aussendungen</label>
            <input id="ft8Repeat" type="number" min="1" max={ft8Options.max_repeats} step="1"
              bind:value={ft8Tx.repeat_count} on:change={selectTxMode} />
          </div>
          <label class="check" title="Gleicht die gemessene Oszillatordrift des Pluto während der Aussendung aus">
            <input type="checkbox" bind:checked={ft8Tx.drift_comp} on:change={selectTxMode} /> Driftkompensation
          </label>
        </div>
      </fieldset>
      <div class="ft8-preview" class:empty={!ft8TxText}>
        {ft8TxText || (stationCall ? "— DX-Rufzeichen fehlt —" : "— eigenes Rufzeichen fehlt (Station) —")}
      </div>
      {#if !clockSynced}<div class="mic-error">Server-Uhr nicht per NTP synchronisiert — FT8-Zeitlage unsicher.</div>{/if}
    {/if}

    {#if txPower}
      <div class="field">
        <label for="txPower">{txPower.label}: {txPower.value.toFixed(2)} {txPower.unit}
          <span class="dim">(bis {txPower.ceiling} {txPower.unit})</span></label>
        <input id="txPower" type="range" min={txPower.min} max={txPower.ceiling} step="0.25"
          value={txPower.value} on:change={(e) => setTxPower(Number(e.currentTarget.value))} />
        {#each txPower.secondary ?? [] as st (st.name)}
          {#if st.kind === "bool"}
            <label class="check secondary-stage" class:on={!!st.value}>
              <input type="checkbox" checked={!!st.value}
                on:change={(e) => client.request("set_gain", { direction: "tx", name: `stage:${st.name}`, value: e.currentTarget.checked ? 1 : 0 })} />
              {st.name === "AMP" ? "RF-Verstärker (+14 dB)" : st.label}
              <span class="dim">— aus bei jedem neuen Verbinden</span>
            </label>
          {/if}
        {/each}
      </div>
    {/if}

    {#if isAudioMode(txMode)}
      <div class="subpanel">
        <div class="subpanel-title">
          Mikrofon
          <button class="small" on:click={toggleMicMonitor}>{micMonitor ? "Pegel aus" : "Pegel anzeigen"}</button>
        </div>
        <div class="meter" title="Mikrofon-Spitzenpegel (dBFS)">
          <div class="meter-fill" class:hot={micLevelDb > -3} style="width: {Math.max(0, Math.min(100, (micLevelDb + 60) / 60 * 100))}%"></div>
        </div>
        <div class="dim">{micLevelDb > -90 ? `${micLevelDb.toFixed(1)} dBFS` : "—"}</div>
      </div>
    {/if}

    {#if txSettings && isVoiceMode(txMode)}
      <div class="subpanel">
        <div class="subpanel-title">Audio-Aufbereitung ({txMode === "fm" ? "FM" : "SSB"})</div>
        <div class="settings-grid">
          <div class="field">
            <label for="nfGain">NF-Pegel {Math.round(txSettings.nf_gain * 100)} %</label>
            <input id="nfGain" type="range" min="0" max="3" step="0.05" value={txSettings.nf_gain}
              on:change={(e) => setTxSetting("nf_gain", Number(e.currentTarget.value))} />
          </div>
          <div class="field">
            <label class="check"><input type="checkbox" checked={txSettings.gate_enabled}
              on:change={(e) => setTxSetting("gate_enabled", e.currentTarget.checked)} /> Rauschsperre (Gate) {txSettings.gate_threshold_db} dB</label>
            <input type="range" min="-80" max="-20" step="1" value={txSettings.gate_threshold_db} disabled={!txSettings.gate_enabled}
              on:change={(e) => setTxSetting("gate_threshold_db", Number(e.currentTarget.value))} />
          </div>
          <div class="field">
            <label class="check"><input type="checkbox" checked={txSettings.compressor_enabled}
              on:change={(e) => setTxSetting("compressor_enabled", e.currentTarget.checked)} /> Kompressor-Schwelle {txSettings.compressor_threshold_db} dB</label>
            <input type="range" min="-40" max="0" step="1" value={txSettings.compressor_threshold_db} disabled={!txSettings.compressor_enabled}
              on:change={(e) => setTxSetting("compressor_threshold_db", Number(e.currentTarget.value))} />
          </div>
          <div class="field">
            <label for="compRatio">Kompressor-Verhältnis {txSettings.compressor_ratio}:1
              {#if compressorGrDb !== null && keyed}· Reduktion {compressorGrDb} dB{/if}</label>
            <input id="compRatio" type="range" min="1" max="10" step="1" value={txSettings.compressor_ratio} disabled={!txSettings.compressor_enabled}
              on:change={(e) => setTxSetting("compressor_ratio", Number(e.currentTarget.value))} />
          </div>
          <label class="check"><input type="checkbox" checked={txSettings.limiter_enabled}
            on:change={(e) => setTxSetting("limiter_enabled", e.currentTarget.checked)} /> Limiter</label>
          {#if txMode === "fm"}
            <div class="field">
              <label for="subtoneLevel">Subton-Pegel {txSettings.subtone_level_pct} %</label>
              <input id="subtoneLevel" type="range" min="5" max="25" step="1" value={txSettings.subtone_level_pct}
                on:change={(e) => setTxSetting("subtone_level_pct", Number(e.currentTarget.value))} />
            </div>
          {/if}
        </div>
      </div>
    {/if}

    {#if txNeedsRearm}
      <div class="row">
        <button class="primary grow" on:click={selectTxMode}>Sender nach NOTAUS wieder aktivieren</button>
      </div>
    {/if}

    <div class="row ptt-row">
      {#if isAudioMode(txMode)}
        <button
          class="ptt"
          class:active={keyed}
          disabled={!txConnected || txNeedsRearm || !wsConnected}
          on:pointerdown|preventDefault={startAudioTx}
          on:pointerup={stopAudioTx}
          on:pointercancel={stopAudioTx}
          on:pointerleave={stopAudioTx}
          on:contextmenu|preventDefault
        >
          {keyed ? "\u{1F534} SENDET — halten" : pttHeld ? "tastet auf…" : "PTT (halten / Leertaste)"}
        </button>
      {:else if txMode === "ft8"}
        <button class="ptt" class:active={keyed} class:armed={!!ft8Armed && !keyed}
          disabled={!txConnected || txNeedsRearm || !wsConnected || (!ft8Armed && !ft8TxText)} on:click={ft8Ptt}>
          {#if keyed}
            {"\u{1F534}"} SENDET {ft8Armed?.of ? `(${ft8Armed.repetition}/${ft8Armed.of})` : ""} — Abbrechen
          {:else if ft8Armed}
            Scharf · Slot {ft8Armed.slot_utc} in {ft8CountdownS} s
            {ft8Armed.of > 1 ? `(${ft8Armed.repetition}/${ft8Armed.of})` : ""} — Abbrechen
          {:else}
            Scharf schalten
          {/if}
        </button>
      {:else}
        <button class="ptt" class:active={keyed} disabled={!txConnected || keyed || txNeedsRearm || !wsConnected} on:click={pocsagPtt}>
          {keyed ? "\u{1F534} SENDET" : "Aussenden"}
        </button>
      {/if}
    </div>
    {#if micError}<div class="mic-error">{micError}</div>{/if}
    {#if txError}<div class="mic-error">Sender: {txError}</div>{/if}
    {#if isAudioMode(txMode) && (keyLatencyMs !== null || txAudioStats)}
      <div class="dim stats">
        {#if keyLatencyMs !== null}<span>Aufgetastet nach {keyLatencyMs} ms</span>{/if}
        {#if txAudioStats}
          <span>
            {txAudioStats.final ? "Letzter Durchgang:" : "Puffer " + txAudioStats.buffer_ms + " ms ·"}
            Mikro {txAudioStats.audio_s} s in {txAudioStats.wall_s} s · max. Lücke {txAudioStats.max_gap_ms} ms ·
            Aussetzer {txAudioStats.underruns} · verworfen {txAudioStats.dropped_ms} ms
          </span>
        {/if}
      </div>
    {/if}
  </section>
</div>

<!-- svelte-ignore a11y-no-static-element-interactions -->
<div class="splitter" title="Ziehen: Höhe von Wasserfall und unterem Bereich ändern · Doppelklick: zurücksetzen"
  on:pointerdown|preventDefault={startResize} on:dblclick={resetBottomHeight}>
  <span></span>
</div>

<section class="panel bottom-panel" style="height: {bottomHeight}px">
  <div class="tabs" role="tablist">
    <button role="tab" class:active={bottomTab === "rx"} aria-selected={bottomTab === "rx"}
      on:click={() => (bottomTab = "rx")}>Empfang — {MODE_LABELS[rxMode] ?? rxMode}</button>
    <button role="tab" class:active={bottomTab === "txlog"} aria-selected={bottomTab === "txlog"}
      on:click={() => (bottomTab = "txlog")}>TX-Verlauf</button>
    <button role="tab" class:active={bottomTab === "events"} aria-selected={bottomTab === "events"}
      on:click={() => (bottomTab = "events")}>Events</button>
  </div>

  <div class="tab-body" class:hidden={bottomTab !== "rx"}>
    <RxDecoder mode={rxMode} connected={rxConnected} rttyText={rttyRxText} {pocsagCalls} {m17Caller} {radeStatus}
      {ft8Slots} {ft8Status} myCall={stationCall}
      onPickDecode={features.ft8?.tx ? pickFt8Decode : null}
      onClearRtty={() => (rttyRxText = "")} />
  </div>

  <div class="tab-body" class:hidden={bottomTab !== "txlog"}>
    <ul class="tx-log">
      {#each txLog as entry (entry.id)}
        <li>
          <span class="mode-tag">{entry.mode.toUpperCase()}</span>
          <span>{fmtFreq(entry.freq_hz)}</span>
          <span class="dim">{fmtTime(entry.started_at)}</span>
          <span class="dim">{fmtDuration(entry.started_at, entry.ended_at)}</span>
          {#if Object.keys(entry.params).length}
            <span class="dim">{JSON.stringify(entry.params)}</span>
          {/if}
        </li>
      {:else}
        <li class="dim">Noch keine Aussendungen.</li>
      {/each}
    </ul>
  </div>

  <div class="tab-body" class:hidden={bottomTab !== "events"}>
    <ul class="events">
      {#each events as e}
        <li>{e}</li>
      {/each}
    </ul>
  </div>
</section>
</div>
{/if}

<style>
  /* The app fills the browser window exactly: header, the RX/TX row
     (takes all free height), and fixed-height logs at the bottom. Only
     the TX column and the logs scroll internally. */
  .app {
    height: 100vh;
    display: flex;
    flex-direction: column;
    gap: 12px;
    padding: 12px 16px;
  }

  .topbar {
    display: flex;
    align-items: center;
    gap: 12px;
    flex: none;
  }
  .spacer {
    flex: 1;
  }
  .station {
    display: flex;
    align-items: center;
    gap: 6px;
  }
  .station input {
    text-transform: uppercase;
    font-family: var(--mono);
  }
  .st-call {
    width: 8em;
  }
  .st-loc {
    width: 6em;
  }
  .status-pill {
    font-size: 0.72rem;
    padding: 3px 10px;
    border-radius: 999px;
    background: var(--danger-dim);
    color: var(--text);
    letter-spacing: 0.03em;
  }
  .status-pill.ok {
    background: #14382a;
    color: var(--ok);
  }

  .layout {
    flex: 1;
    min-height: 0;
    display: grid;
    grid-template-columns: minmax(0, 3fr) minmax(0, 2fr);
    gap: 12px;
  }
  /* Grid items default to a min-width of their content's min-content size,
     which lets an unwrapped row of controls blow out the 340px TX column
     past the viewport -- min-width: 0 lets the track's own width win, and
     .row wraps so controls reflow instead of overflowing it. */
  .rx-panel,
  .tx-panel {
    min-width: 0;
    min-height: 0;
  }
  .rx-panel {
    display: flex;
    flex-direction: column;
  }
  .wf-area {
    flex: 1;
    min-height: 220px;
  }
  .tx-panel {
    overflow-y: auto;
  }
  .wf-controls {
    margin: 8px 0 0;
    align-items: flex-end;
  }
  .secondary-stage {
    margin-top: 6px;
  }
  .secondary-stage.on {
    color: #f5c211;
  }
  .volume input[type="range"] {
    width: 120px;
  }
  .rx-gain {
    align-items: flex-end;
  }
  /* RX frequency row: every box on one line -- bottoms aligned, one control height, and each
     control under a label line, so the large frequency display spans label + control exactly. */
  .rx-freq-row {
    align-items: flex-end;
    --ctl-h: 34px;
  }
  .rx-freq-row .field select,
  .rx-freq-row .field button,
  .rx-freq-row .field input[type="number"],
  .rx-freq-row .field .check {
    height: var(--ctl-h);
    box-sizing: border-box;
  }
  .rx-freq-row .field .check {
    display: flex;
    align-items: center;
    gap: 6px;
  }
  .rx-freq-row .field input[type="range"] {
    margin: calc((var(--ctl-h) - 4px) / 2) 0;  /* the 4px track, centred in the control height */
  }
  .rx-freq-row .field label.check {
    text-transform: none;
    letter-spacing: normal;
    font-size: 0.85rem;
    color: var(--text);
  }
  .rx-freq-row .field-spacer {
    font-size: 0.7rem;
  }
  .rx-freq-row > .dim {
    line-height: var(--ctl-h);
  }
  .ft8-row {
    align-items: flex-end;
  }
  .slot-clock {
    flex: 1;
    min-width: 200px;
  }
  .slot-label {
    font-family: var(--mono);
    font-size: 0.8rem;
    margin-bottom: 3px;
  }
  .slot-bar {
    height: 6px;
    background: var(--bg-2);
    border-radius: 3px;
    overflow: hidden;
  }
  .slot-fill {
    height: 100%;
    background: var(--accent);
  }
  .slot-fill.odd {
    background: #f5c211;
  }
  .squelch {
    min-width: 220px;
    flex: 1;
  }
  .sq-dot {
    display: inline-block;
    width: 8px;
    height: 8px;
    border-radius: 50%;
    background: var(--border-light);
    margin-right: 4px;
  }
  .sq-dot.open {
    background: var(--ok);
  }
  .level {
    position: relative;
    height: 6px;
    background: var(--bg-2);
    border-radius: 3px;
    overflow: hidden;
  }
  .level-fill {
    height: 100%;
    background: var(--text-dim);
    transition: width 150ms linear;
  }
  .level-fill.open {
    background: var(--ok);
  }
  .level-mark {
    position: absolute;
    top: 0;
    bottom: 0;
    width: 2px;
    background: var(--danger);
  }
  .rx-gain .field.grow {
    min-width: 160px;
  }
  input.ppm {
    width: 5.5em;
  }
  .wf-controls .field.narrow {
    flex: none;
    min-width: 0;
  }
  .wf-controls .auto-level {
    margin-left: auto;
    flex: none;
    padding-bottom: 2px;
  }
  /* Narrow screens: stacked, and the page scrolls normally instead. */
  @media (max-width: 1100px) {
    .app {
      height: auto;
      min-height: 100vh;
    }
    .layout {
      grid-template-columns: 1fr;
    }
    .wf-area {
      flex: none;
      height: 420px;
    }
    .tx-panel {
      overflow-y: visible;
    }
  }

  .row {
    display: flex;
    align-items: center;
    gap: 8px;
    margin-bottom: 10px;
    flex-wrap: wrap;
  }
  .row.sliders {
    align-items: stretch;
    gap: 16px;
  }
  .row.sliders .field {
    flex: 1;
    min-width: 90px;
  }

  select,
  input[type="text"],
  input[type="number"] {
    min-width: 0;
  }

  .ptt-row {
    margin-top: 4px;
  }
  button.ptt {
    flex: 1;
    padding: 10px;
    font-weight: 700;
    letter-spacing: 0.03em;
    background: var(--bg-2);
    border: 1px solid var(--border-light);
  }
  button.ptt.active {
    background: var(--danger);
    border-color: var(--danger);
    color: #fff;
    box-shadow: 0 0 14px rgba(255, 77, 77, 0.6);
  }
  /* FT8 series armed: waiting for its slot */
  button.ptt.armed {
    background: #5c3d0f;
    border-color: #f5c211;
    color: #f5c211;
  }
  fieldset.ft8-tx {
    border: none;
    margin: 0;
    padding: 0;
    min-width: 0;
    display: flex;
    flex-direction: column;
    gap: inherit;
  }
  .ft8-tx input.callsign {
    text-transform: uppercase;
    width: 9em;
  }
  .ft8-preview {
    font-family: var(--mono);
    font-size: 0.95rem;
    padding: 4px 8px;
    background: var(--bg-0);
    border: 1px solid var(--border);
    border-radius: var(--radius);
  }
  .ft8-preview.empty {
    color: var(--text-dim);
    font-size: 0.8rem;
  }

  .tx-panel.keyed {
    border-color: var(--danger);
    box-shadow: 0 0 0 1px var(--danger-dim);
  }
  .tx-panel > .field,
  .subpanel {
    margin-bottom: 12px;
  }
  .subpanel {
    border: 1px solid var(--border);
    border-radius: var(--radius);
    padding: 10px 12px;
    background: var(--bg-0);
  }
  .subpanel-title {
    display: flex;
    align-items: center;
    justify-content: space-between;
    font-size: 0.72rem;
    text-transform: uppercase;
    letter-spacing: 0.06em;
    color: var(--text-dim);
    margin-bottom: 8px;
  }
  .settings-grid {
    display: grid;
    grid-template-columns: repeat(auto-fill, minmax(210px, 1fr));
    gap: 10px 18px;
    align-items: end;
  }
  .field {
    display: flex;
    flex-direction: column;
    gap: 4px;
  }
  .field.grow,
  .grow {
    flex: 1;
  }
  .field input[type="range"] {
    width: 100%;
  }
  button.small {
    font-size: 0.72rem;
    padding: 2px 8px;
  }
  .meter {
    height: 10px;
    background: var(--bg-2);
    border-radius: 5px;
    overflow: hidden;
    margin-bottom: 4px;
  }
  .meter-fill {
    height: 100%;
    background: linear-gradient(90deg, #1f8a4c, #33d17a 70%, #f5c211);
    transition: width 60ms linear;
  }
  .meter-fill.hot {
    background: var(--danger);
  }
  .hint {
    margin-top: 6px;
  }
  .stats {
    display: flex;
    flex-direction: column;
    gap: 2px;
  }
  button.ptt {
    min-height: 64px;
    font-size: 1.05rem;
  }

  .notices {
    flex: none;
    display: flex;
    flex-direction: column;
    gap: 6px;
  }
  .notice {
    text-align: left;
    padding: 8px 12px;
    border-radius: var(--radius);
    border: 1px solid var(--danger);
    background: var(--danger-dim);
    color: var(--text);
    font-size: 0.85rem;
  }
  .notice.info {
    border-color: var(--ok);
    background: #14382a;
  }

  .mic-error {
    color: var(--danger);
    font-size: 0.75rem;
    margin-top: 4px;
  }

  .events {
    overflow-y: auto;
    font-family: var(--mono);
    font-size: 0.72rem;
    list-style: none;
    padding: 0;
    margin: 0;
    color: var(--text-dim);
  }
  .events li {
    padding: 2px 0;
    border-bottom: 1px solid var(--border);
  }

  .loading {
    display: flex;
    align-items: center;
    justify-content: center;
    min-height: 100vh;
    color: var(--text-dim);
  }

  /* Divider between the RX/TX row and the bottom panel: dragging it moves
     height between the waterfall (flex: 1) and the tabs. It sits in the
     .app gap, pulled up/down by negative margins so it adds no space. */
  .splitter {
    flex: none;
    height: 12px;
    margin: -12px 0;
    cursor: row-resize;
    display: flex;
    align-items: center;
    justify-content: center;
    touch-action: none;
    z-index: 1;
  }
  .splitter span {
    width: 48px;
    height: 4px;
    border-radius: 2px;
    background: var(--border-light);
  }
  .splitter:hover span {
    background: var(--accent);
  }
  .bottom-panel {
    flex: none;
    display: flex;
    flex-direction: column;
    overflow: hidden;
    padding-top: 8px;
  }
  .tabs {
    display: flex;
    gap: 4px;
    border-bottom: 1px solid var(--border);
    margin-bottom: 8px;
    flex: none;
  }
  .tabs button {
    background: none;
    border: none;
    border-bottom: 2px solid transparent;
    border-radius: 0;
    padding: 4px 12px 6px;
    color: var(--text-dim);
    font-size: 0.78rem;
    letter-spacing: 0.04em;
    text-transform: uppercase;
    font-weight: 600;
  }
  .tabs button:hover {
    color: var(--text);
  }
  .tabs button.active {
    color: var(--text);
    border-bottom-color: var(--accent);
  }
  .tab-body {
    flex: 1;
    min-height: 0;
    display: flex;
    flex-direction: column;
  }
  .tab-body.hidden {
    display: none;
  }
  .tab-body ul {
    flex: 1;
    min-height: 0;
  }

  .tx-log {
    overflow-y: auto;
    font-size: 0.78rem;
    list-style: none;
    padding: 0;
    margin: 0;
  }
  .tx-log li {
    display: flex;
    flex-wrap: wrap;
    gap: 8px;
    align-items: center;
    padding: 4px 0;
    border-bottom: 1px solid var(--border);
  }
  .mode-tag {
    font-family: var(--mono);
    font-weight: 700;
    color: var(--accent);
    min-width: 4.5em;
  }
  .dim {
    color: var(--text-dim);
    font-size: 0.75rem;
  }
</style>
