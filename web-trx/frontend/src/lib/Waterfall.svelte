<script lang="ts">
  import { onDestroy, onMount } from "svelte";

  // Follows the container's width (bind:clientWidth below); resizing the
  // canvas clears it, the waterfall just refills from the next rows.
  let width = 900;
  // Total height available (spectrum + waterfall); the parent binds it to
  // the free space in its panel so the display fills the window.
  export let height = 420;
  export let floorDb = -100;
  export let ceilingDb = -20;
  // Display-only zoom: crops each row to its centre 1/zoom. Only used with
  // backends that can't zoom themselves (SimBackend) -- GnuRadioBackend
  // runs a real zoom-FFT (features.fft_zoom_max), then this stays 1.
  export let zoom = 1;
  // Called with the frequency (Hz) the user clicked, computed from the
  // most recently rendered row's center/span and the current zoom crop.
  export let onClickFreq: ((freqHz: number) => void) | undefined = undefined;
  // Auto-level: tracks the noise floor (a low percentile of each row,
  // smoothed) and places floor/ceiling relative to it, reported through
  // onAutoLevel so the sliders follow. Device dB values are uncalibrated
  // (Pluto's noise sits around -45 dB, SimBackend's around -95 dB), so a
  // fixed default range can't fit every device.
  export let autoLevel = true;
  // Vertical lines in the spectrum (e.g. RX and TX frequency), drawn when
  // inside the displayed span.
  // widthHz: draw a translucent band [hz, hz + widthHz] instead of a line
  // (e.g. the FT8 audio band above the USB dial frequency).
  // edgeColor: also draw the band's two edges as thin lines, in the spectrum and (not scrolling)
  // over the waterfall -- the receive bandwidth, like pluto-advanced-rx's demod-band shading.
  export let markers: Array<{ hz: number; color: string; label: string; widthHz?: number; edgeColor?: string }> = [];
  export let onAutoLevel: ((floorDb: number, ceilingDb: number) => void) | undefined = undefined;
  const AUTO_BELOW_NOISE_DB = 5;
  const AUTO_RANGE_DB = 45;
  let noiseDb: number | null = null;
  let rowsSinceAuto = 0;

  const spectrumHeight = 110;
  // Bottom strip of the spectrum canvas holds the frequency scale; the trace uses the rest.
  const AXIS_H = 16;
  const plotHeight = spectrumHeight - AXIS_H;
  $: waterfallHeight = Math.max(60, Math.floor(height) - spectrumHeight - 6);

  let waterfallCanvas: HTMLCanvasElement;
  let spectrumCanvas: HTMLCanvasElement;
  let wCtx: CanvasRenderingContext2D;
  let sCtx: CanvasRenderingContext2D;
  let lastCenterHz = 0;
  let lastSpanHz = 0;

  // Frequency -> x pixel of the current (zoomed) view; shared by markers, ticks and the band overlay.
  function hzToX(hz: number, centerHz: number, spanHz: number): number {
    const shown = spanHz / zoom;
    return ((hz - (centerHz - shown / 2)) / shown) * width;
  }

  // Band edges over the waterfall (HTML overlay, so they don't scroll with the image).
  // (width and zoom passed explicitly so Svelte re-runs this when they change, too)
  $: bandOverlays = bandEdges(markers, lastCenterHz, lastSpanHz, width, zoom);
  function bandEdges(ms: typeof markers, centerHz: number, spanHz: number, w: number, _z: number) {
    if (!spanHz) return [];
    return ms
      .filter((m) => m.widthHz && m.edgeColor)
      .map((m) => ({
        left: Math.max(0, hzToX(m.hz, centerHz, spanHz)),
        right: Math.min(w, hzToX(m.hz + (m.widthHz ?? 0), centerHz, spanHz)),
        color: m.edgeColor!,
      }))
      .filter((b) => b.right > 0 && b.left < w);
  }

  // "Nice" tick step (1/2/5 * 10^n Hz) for about eight labels, at least MIN_TICK_PX apart.
  const MIN_TICK_PX = 70;
  function tickStep(spanHz: number): number {
    const raw = spanHz / 8;
    let mag = 10 ** Math.floor(Math.log10(raw));
    for (;;) {
      for (const f of [1, 2, 5]) {
        const step = f * mag;
        if (step >= raw * 0.75 && (step / spanHz) * width >= MIN_TICK_PX) return step;
      }
      mag *= 10;
    }
  }

  function tickLabel(hz: number, step: number): string {
    const decimals = Math.max(0, Math.ceil(-Math.log10(step / 1e6) - 1e-9));
    return (hz / 1e6).toFixed(decimals).replace(".", ",");
  }

  function drawTicks(): void {
    const shown = lastSpanHz / zoom;
    if (!shown) return;
    const step = tickStep(shown);
    const lo = lastCenterHz - shown / 2;
    sCtx.fillStyle = "#070b12";
    sCtx.fillRect(0, plotHeight, width, AXIS_H);
    sCtx.font = "10px sans-serif";
    sCtx.textAlign = "center";
    sCtx.fillStyle = "rgba(200, 210, 230, 0.5)";
    sCtx.strokeStyle = "rgba(200, 210, 230, 0.35)";
    sCtx.lineWidth = 1;
    for (let hz = Math.ceil(lo / step) * step; hz <= lo + shown; hz += step) {
      const x = Math.round(hzToX(hz, lastCenterHz, lastSpanHz)) + 0.5;
      sCtx.beginPath();
      sCtx.moveTo(x, plotHeight);
      sCtx.lineTo(x, plotHeight + 4);
      sCtx.stroke();
      if (x > 20 && x < width - 20) sCtx.fillText(tickLabel(hz, step), x, spectrumHeight - 3);
    }
    sCtx.textAlign = "start";
  }

  onMount(() => {
    wCtx = waterfallCanvas.getContext("2d")!;
    sCtx = spectrumCanvas.getContext("2d")!;
    wCtx.fillStyle = "#04070c";
    wCtx.fillRect(0, 0, width, waterfallHeight);
    rafId = requestAnimationFrame(drawLoop);
  });
  onDestroy(() => cancelAnimationFrame(rafId));

  // Rows arrive unevenly (device USB bursts, network jitter): queue them and draw at the measured
  // average row rate instead of the moment they arrive, so the waterfall scrolls smoothly. The
  // queue stays short -- when it grows, rows are drawn faster until it has caught up.
  type QueuedRow = { row: Float32Array; centerHz?: number; spanHz?: number };
  const queue: QueuedRow[] = [];
  const MAX_QUEUE = 8;
  let rafId = 0;
  let lastArrivalMs = 0;
  let rowIntervalMs = 40;
  let nextDrawMs = 0;

  export function enqueueRow(row: Float32Array, centerHz?: number, spanHz?: number): void {
    const now = performance.now();
    if (lastArrivalMs) {
      const dt = Math.min(250, Math.max(5, now - lastArrivalMs));
      rowIntervalMs += 0.05 * (dt - rowIntervalMs);
    }
    lastArrivalMs = now;
    if (queue.length === 0 && now > nextDrawMs + rowIntervalMs) nextDrawMs = now;  // was idle: start fresh
    queue.push({ row, centerHz, spanHz });
    if (queue.length > MAX_QUEUE) queue.splice(0, queue.length - MAX_QUEUE);
  }

  function drawLoop(now: number): void {
    if (queue.length && now >= nextDrawMs) {
      const q = queue.shift()!;
      pushRow(q.row, q.centerHz, q.spanHz);
      const catchUp = queue.length > 2 ? 0.7 : 1;
      nextDrawMs = Math.max(nextDrawMs + rowIntervalMs * catchUp, now - rowIntervalMs);
    }
    rafId = requestAnimationFrame(drawLoop);
  }

  // SDR++-like palette: dark blue noise floor through cyan/green to a
  // yellow/red hot peak.
  const STOPS: Array<[number, [number, number, number]]> = [
    [0.0, [6, 10, 40]],
    [0.3, [10, 70, 140]],
    [0.55, [20, 180, 170]],
    [0.78, [240, 220, 60]],
    [1.0, [235, 40, 40]],
  ];

  function dbToColor(db: number): [number, number, number] {
    const t = Math.min(1, Math.max(0, (db - floorDb) / (ceilingDb - floorDb)));
    for (let i = 1; i < STOPS.length; i++) {
      const [t0, c0] = STOPS[i - 1];
      const [t1, c1] = STOPS[i];
      if (t <= t1) {
        const f = (t - t0) / (t1 - t0 || 1);
        return [
          Math.round(c0[0] + (c1[0] - c0[0]) * f),
          Math.round(c0[1] + (c1[1] - c0[1]) * f),
          Math.round(c0[2] + (c1[2] - c0[2]) * f),
        ];
      }
    }
    return STOPS[STOPS.length - 1][1];
  }

  function trackNoise(row: Float32Array): void {
    const sorted = Float32Array.from(row).sort();
    const p20 = sorted[Math.floor(sorted.length * 0.2)];
    const first = noiseDb === null;
    const noise = noiseDb === null ? p20 : noiseDb + 0.1 * (p20 - noiseDb);
    noiseDb = noise;
    if ((first || ++rowsSinceAuto >= 10) && onAutoLevel) {
      rowsSinceAuto = 0;
      const floor = Math.round(noise - AUTO_BELOW_NOISE_DB);
      onAutoLevel(floor, floor + AUTO_RANGE_DB);
      // Apply right away for this row too -- the props only update after
      // the parent re-renders, one row late.
      floorDb = floor;
      ceilingDb = floor + AUTO_RANGE_DB;
    }
  }

  // Strongest bin per pixel column -- plain sampling would drop narrow
  // carriers whenever there are more bins than pixels.
  function binsToPixels(slice: Float32Array): Float32Array {
    const n = slice.length;
    const px = new Float32Array(width);
    for (let x = 0; x < width; x++) {
      const lo = Math.floor((x / width) * n);
      const hi = Math.max(lo + 1, Math.floor(((x + 1) / width) * n));
      let m = slice[lo];
      for (let b = lo + 1; b < hi; b++) if (slice[b] > m) m = slice[b];
      px[x] = m;
    }
    return px;
  }

  function zoomedSlice(row: Float32Array): Float32Array {
    if (zoom <= 1) return row;
    const n = row.length;
    const keep = Math.max(8, Math.round(n / zoom));
    const start = Math.floor((n - keep) / 2);
    return row.subarray(start, start + keep);
  }

  export function pushRow(row: Float32Array, centerHz?: number, spanHz?: number): void {
    if (!wCtx || !sCtx) return;
    if (centerHz !== undefined) lastCenterHz = centerHz;
    if (spanHz !== undefined) lastSpanHz = spanHz;

    if (autoLevel) trackNoise(row);
    const px = binsToPixels(zoomedSlice(row));

    // Scroll the waterfall down by one line, draw the new row at the top.
    wCtx.drawImage(waterfallCanvas, 0, 0, width, waterfallHeight - 1, 0, 1, width, waterfallHeight - 1);
    const lineImage = wCtx.createImageData(width, 1);
    for (let x = 0; x < width; x++) {
      const [r, g, b] = dbToColor(px[x]);
      const i = x * 4;
      lineImage.data[i] = r;
      lineImage.data[i + 1] = g;
      lineImage.data[i + 2] = b;
      lineImage.data[i + 3] = 255;
    }
    wCtx.putImageData(lineImage, 0, 0);

    sCtx.fillStyle = "#04070c";
    sCtx.fillRect(0, 0, width, spectrumHeight);
    sCtx.strokeStyle = "#5ad1ff";
    sCtx.lineWidth = 1.25;
    sCtx.beginPath();
    for (let x = 0; x < width; x++) {
      const t = Math.min(1, Math.max(0, (px[x] - floorDb) / (ceilingDb - floorDb)));
      const y = plotHeight - t * plotHeight;
      if (x === 0) sCtx.moveTo(x, y);
      else sCtx.lineTo(x, y);
    }
    sCtx.stroke();

    drawTicks();
    const displayedSpanHz = lastSpanHz / zoom;
    sCtx.font = "11px sans-serif";
    for (const m of markers) {
      if (!displayedSpanHz) break;
      const x = Math.round(hzToX(m.hz, lastCenterHz, lastSpanHz)) + 0.5;
      if (m.widthHz) {
        const x2 = Math.round(hzToX(m.hz + m.widthHz, lastCenterHz, lastSpanHz)) + 0.5;
        if (x2 < 0 || x > width) continue;
        sCtx.fillStyle = m.color;
        sCtx.fillRect(Math.max(0, x), 0, Math.max(1, Math.min(width, x2) - Math.max(0, x)), plotHeight);
        if (m.edgeColor) {
          sCtx.strokeStyle = m.edgeColor;
          sCtx.lineWidth = 1;
          sCtx.beginPath();
          for (const ex of [x, x2]) {
            if (ex < 0 || ex > width) continue;
            sCtx.moveTo(ex, 0);
            sCtx.lineTo(ex, plotHeight);
          }
          sCtx.stroke();
        }
        if (m.label) {
          sCtx.fillStyle = m.edgeColor ?? m.color;
          sCtx.fillText(m.label, Math.max(0, x) + 4, 24);
        }
        continue;
      }
      if (x < 0 || x > width) continue;
      sCtx.strokeStyle = m.color;
      sCtx.fillStyle = m.color;
      sCtx.setLineDash([4, 3]);
      sCtx.beginPath();
      sCtx.moveTo(x, 0);
      sCtx.lineTo(x, plotHeight);
      sCtx.stroke();
      sCtx.setLineDash([]);
      sCtx.fillText(m.label, Math.min(x + 4, width - 24), 12);
    }
  }

  function handleClick(ev: MouseEvent, canvas: HTMLCanvasElement): void {
    if (!onClickFreq || lastSpanHz === 0) return;
    const rect = canvas.getBoundingClientRect();
    const xFrac = (ev.clientX - rect.left) / rect.width; // 0..1 across the CURRENT (zoomed) view
    const displayedSpanHz = lastSpanHz / zoom;
    const loHz = lastCenterHz - displayedSpanHz / 2;
    onClickFreq(loHz + xFrac * displayedSpanHz);
  }
</script>

<div class="waterfall-wrap" bind:clientWidth={width}>
  <canvas
    bind:this={spectrumCanvas}
    width={width}
    height={spectrumHeight}
    class="clickable"
    on:click={(e) => handleClick(e, spectrumCanvas)}
  ></canvas>
  <div class="wf-holder">
    <canvas
      bind:this={waterfallCanvas}
      width={width}
      height={waterfallHeight}
      class="clickable"
      on:click={(e) => handleClick(e, waterfallCanvas)}
    ></canvas>
    {#each bandOverlays as b}
      <div class="band" style="left: {b.left}px; width: {Math.max(1, b.right - b.left)}px; --edge: {b.color};"></div>
    {/each}
  </div>
</div>

<style>
  .waterfall-wrap {
    display: flex;
    flex-direction: column;
    gap: 2px;
    background: #000;
    border: 1px solid var(--border);
    border-radius: 8px;
    overflow: hidden;
    width: 100%;
  }
  canvas {
    display: block;
    width: 100%;
  }
  canvas.clickable {
    cursor: crosshair;
  }
  .wf-holder {
    position: relative;
  }
  .band {
    position: absolute;
    top: 0;
    bottom: 0;
    pointer-events: none;
    border-left: 1px solid var(--edge);
    border-right: 1px solid var(--edge);
    opacity: 0.55;
  }
</style>
