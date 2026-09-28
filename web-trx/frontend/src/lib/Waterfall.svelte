<script lang="ts">
  import { onMount } from "svelte";

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
  export let markers: Array<{ hz: number; color: string; label: string; widthHz?: number }> = [];
  export let onAutoLevel: ((floorDb: number, ceilingDb: number) => void) | undefined = undefined;
  const AUTO_BELOW_NOISE_DB = 5;
  const AUTO_RANGE_DB = 45;
  let noiseDb: number | null = null;
  let rowsSinceAuto = 0;

  const spectrumHeight = 110;
  $: waterfallHeight = Math.max(60, Math.floor(height) - spectrumHeight - 6);

  let waterfallCanvas: HTMLCanvasElement;
  let spectrumCanvas: HTMLCanvasElement;
  let wCtx: CanvasRenderingContext2D;
  let sCtx: CanvasRenderingContext2D;
  let lastCenterHz = 0;
  let lastSpanHz = 0;

  onMount(() => {
    wCtx = waterfallCanvas.getContext("2d")!;
    sCtx = spectrumCanvas.getContext("2d")!;
    wCtx.fillStyle = "#04070c";
    wCtx.fillRect(0, 0, width, waterfallHeight);
  });

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
      const y = spectrumHeight - t * spectrumHeight;
      if (x === 0) sCtx.moveTo(x, y);
      else sCtx.lineTo(x, y);
    }
    sCtx.stroke();

    const displayedSpanHz = lastSpanHz / zoom;
    const loHz = lastCenterHz - displayedSpanHz / 2;
    sCtx.font = "11px sans-serif";
    for (const m of markers) {
      if (!displayedSpanHz) break;
      const x = Math.round(((m.hz - loHz) / displayedSpanHz) * width) + 0.5;
      if (m.widthHz) {
        const x2 = Math.round(((m.hz + m.widthHz - loHz) / displayedSpanHz) * width);
        if (x2 < 0 || x > width) continue;
        sCtx.fillStyle = m.color;
        sCtx.fillRect(Math.max(0, x), 0, Math.max(1, Math.min(width, x2) - Math.max(0, x)), spectrumHeight);
        sCtx.fillText(m.label, Math.max(0, x) + 4, spectrumHeight - 4);
        continue;
      }
      if (x < 0 || x > width) continue;
      sCtx.strokeStyle = m.color;
      sCtx.fillStyle = m.color;
      sCtx.setLineDash([4, 3]);
      sCtx.beginPath();
      sCtx.moveTo(x, 0);
      sCtx.lineTo(x, spectrumHeight);
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
  <canvas
    bind:this={waterfallCanvas}
    width={width}
    height={waterfallHeight}
    class="clickable"
    on:click={(e) => handleClick(e, waterfallCanvas)}
  ></canvas>
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
</style>
