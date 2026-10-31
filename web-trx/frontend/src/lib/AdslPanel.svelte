<script context="module" lang="ts">
  // adsl_packet / hello events (web_trx/radio_backend.py, pluto_tx/adsl_codec.py event_fields()).
  export type AdslAircraft = {
    label: string; packets: number; heard_ms: number; position_ms: number | null;
    trail: [number, number, number][];
    lat?: number | null; lon?: number | null; alt_m?: number | null; speed_mps?: number | null;
    climb_mps?: number | null; track_deg?: number | null; category?: string; flight_state?: string;
    emergency?: string; channel_hz?: number; frequency_offset_hz?: number;
  };
  export type AdslStatus = { channels: { channel_hz: number; packets: number; crc_failures: number }[] };
</script>

<script lang="ts">
  import { onDestroy, onMount, tick } from "svelte";
  import AdslMap from "./AdslMap.svelte";
  import { bearingDeg, distanceKm, type LatLon } from "./geo";

  // ADS-L receive view: live map plus the list of aircraft. A click on a row or marker selects that aircraft.

  export let aircraft: AdslAircraft[] = [];
  export let status: AdslStatus | null = null;
  export let station: LatLon | null = null;
  export let connected = false;
  export let tileSetting = "";
  export let onClear: () => void = () => {};

  let selected: string | null = null;
  let fullscreen = false;
  let now = Date.now();
  let map: AdslMap;
  let timer: ReturnType<typeof setInterval>;

  const isEmergency = (a: AdslAircraft): boolean => !!a.emergency && a.emergency !== "none" && a.emergency !== "undefined";
  // Emergencies on top, otherwise a stable order: rows must not jump while being read.
  $: rows = [...aircraft].sort((a, b) => Number(isEmergency(b)) - Number(isEmergency(a)) || a.label.localeCompare(b.label));
  $: packets = status?.channels.reduce((sum, c) => sum + c.packets, 0) ?? 0;
  $: crcFailures = status?.channels.reduce((sum, c) => sum + c.crc_failures, 0) ?? 0;

  const fixed = (v: number | null | undefined, digits = 0, scale = 1): string => (v == null ? "" : (v * scale).toFixed(digits));
  const signed = (v: number | null | undefined): string => (v == null ? "" : `${v > 0 ? "+" : ""}${v.toFixed(1)}`);
  function position(a: AdslAircraft): LatLon | null {
    return a.lat != null && a.lon != null ? [a.lat, a.lon] : null;
  }
  function distance(a: AdslAircraft): string {
    const p = position(a);
    return p && station ? distanceKm(station, p).toFixed(1) : "";
  }
  function bearing(a: AdslAircraft): string {
    const p = position(a);
    return p && station ? bearingDeg(station, p).toFixed(0) : "";
  }

  async function toggleFullscreen(): Promise<void> {
    fullscreen = !fullscreen;
    await tick();
    map?.resize();
  }
  function onKey(e: KeyboardEvent): void {
    if (e.key === "Escape" && fullscreen) toggleFullscreen();
  }

  onMount(() => {
    timer = setInterval(() => (now = Date.now()), 1000);
  });
  onDestroy(() => clearInterval(timer));
</script>

<svelte:window on:keydown={onKey} />

<div class="adsl" class:fullscreen>
  <div class="head">
    <span class="dim">
      {#if !connected}Empfänger nicht verbunden — letzter Stand.{:else}868,2 + 868,4 MHz · {packets} Pakete · {crcFailures} CRC-Fehler{/if}
      · {aircraft.length} Luftfahrzeuge
    </span>
    <span class="notice">Nur zur Beobachtung, nicht zur Navigation oder Kollisionsvermeidung.</span>
    <span class="buttons">
      <button class="small" on:click={() => map?.fitAll()}>Alle zeigen</button>
      <button class="small" on:click={toggleFullscreen}>{fullscreen ? "Vollbild aus" : "Vollbild"}</button>
      <button class="small" on:click={onClear}>Leeren</button>
    </span>
  </div>
  <div class="body">
    <div class="map-box">
      <AdslMap bind:this={map} {aircraft} {station} {selected} {now} {tileSetting} onSelect={(label) => (selected = label)} />
    </div>
    <div class="list">
      <table>
        <thead>
          <tr>
            <th>Adresse</th><th>Kategorie</th><th title="Über dem WGS-84-Ellipsoid, nicht über Grund oder Meer">Höhe m (Ellipsoid)</th>
            <th>km/h</th><th>m/s</th><th>Kurs</th><th>km</th><th>Peilung</th><th>Alter s</th><th>Pakete</th>
          </tr>
        </thead>
        <tbody>
          {#each rows as a (a.label)}
            <tr class:selected={a.label === selected} class:emergency={isEmergency(a)} class:no-position={!position(a)}
              on:click={() => (selected = selected === a.label ? null : a.label)}
              title={isEmergency(a) ? `Notfall: ${a.emergency}` : position(a) ? "" : "Keine Position gemeldet: nicht auf der Karte"}>
              <td class="mono">{a.label}</td><td>{a.category ?? ""}</td><td>{fixed(a.alt_m)}</td>
              <td>{fixed(a.speed_mps, 0, 3.6)}</td><td>{signed(a.climb_mps)}</td><td>{fixed(a.track_deg)}</td>
              <td>{distance(a)}</td><td>{bearing(a)}</td><td>{Math.max(0, Math.round((now - a.heard_ms) / 1000))}</td>
              <td>{a.packets}</td>
            </tr>
          {:else}
            <tr><td colspan="10" class="dim">Noch keine Pakete empfangen.</td></tr>
          {/each}
        </tbody>
      </table>
    </div>
  </div>
</div>

<style>
  .adsl {
    flex: 1;
    min-height: 0;
    display: flex;
    flex-direction: column;
    gap: 4px;
  }
  .adsl.fullscreen {
    position: fixed;
    inset: 0;
    z-index: 2000;
    padding: 8px;
    background: var(--bg-0);
  }
  .head {
    display: flex;
    flex-wrap: wrap;
    justify-content: space-between;
    align-items: center;
    gap: 4px 12px;
    font-size: 0.78rem;
  }
  .dim {
    color: var(--text-dim);
  }
  .notice {
    color: #ffd166;
  }
  .buttons {
    display: flex;
    gap: 4px;
  }
  button.small {
    font-size: 0.72rem;
    padding: 2px 8px;
  }
  .body {
    flex: 1;
    min-height: 0;
    display: flex;
    gap: 6px;
  }
  .map-box {
    flex: 1 1 55%;
    min-width: 0;
    isolation: isolate; /* Leaflet's z-indexes stay inside the map */
  }
  .list {
    flex: 1 1 45%;
    min-width: 0;
    overflow: auto;
  }
  table {
    width: 100%;
    border-collapse: collapse;
    font-size: 0.75rem;
  }
  th {
    position: sticky;
    top: 0;
    background: var(--bg-1);
    color: var(--text-dim);
    font-weight: normal;
    text-align: right;
    padding: 2px 6px;
    white-space: nowrap;
  }
  td {
    text-align: right;
    padding: 2px 6px;
    white-space: nowrap;
    border-top: 1px solid var(--border);
  }
  th:nth-child(-n + 2),
  td:nth-child(-n + 2) {
    text-align: left;
  }
  tbody tr {
    cursor: pointer;
  }
  tbody tr:hover {
    background: var(--bg-2);
  }
  tr.selected {
    background: var(--bg-3);
    color: #ffd166;
  }
  tr.emergency td {
    color: #fff;
    background: var(--danger-dim);
  }
  tr.no-position td {
    color: var(--text-dim);
  }
  .mono {
    font-family: var(--mono);
  }
  @media (max-width: 1100px) {
    .body {
      flex-direction: column;
    }
    .map-box {
      flex: 1 1 60%;
      min-height: 180px;
    }
    .list {
      flex: 1 1 40%;
    }
  }
</style>
