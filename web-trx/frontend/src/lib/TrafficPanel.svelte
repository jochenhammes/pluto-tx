<script lang="ts">
  import { onDestroy, onMount, tick } from "svelte";
  import TrafficMap from "./TrafficMap.svelte";
  import type { LatLon } from "./geo";
  import type { TrafficAircraft, TrafficView } from "./trafficViews";

  // Aircraft receive view for ADS-L and ADS-B: live map plus the list of aircraft. A click on a row or marker selects
  // that aircraft. What the columns show comes from the mode's TrafficView.

  export let view: TrafficView;
  export let aircraft: TrafficAircraft[] = [];
  export let headline = "";
  export let station: LatLon | null = null;
  export let connected = false;
  export let tileSetting = "";
  export let onClear: () => void = () => {};

  let selected: string | null = null;
  let fullscreen = false;
  let now = Date.now();
  let map: TrafficMap;
  let timer: ReturnType<typeof setInterval>;

  const isEmergency = (a: TrafficAircraft): boolean => !!a.emergency && a.emergency !== "none" && a.emergency !== "undefined";
  // Emergencies on top, otherwise a stable order: rows must not jump while being read.
  $: rows = [...aircraft].sort((a, b) => Number(isEmergency(b)) - Number(isEmergency(a)) || a.label.localeCompare(b.label));
  const hasPosition = (a: TrafficAircraft): boolean => a.lat != null && a.lon != null;

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

<div class="traffic" class:fullscreen>
  <div class="head">
    <span class="dim">
      {#if !connected}Empfänger nicht verbunden — letzter Stand.{:else}{headline}{/if}
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
      <TrafficMap bind:this={map} {aircraft} {station} {selected} {now} {tileSetting} rings={view.rings} tagText={view.tagText}
        onSelect={(label) => (selected = label)} />
    </div>
    <div class="list">
      <table>
        <thead>
          <tr>{#each view.columns as column}<th title={column.tooltip ?? ""}>{column.title}</th>{/each}</tr>
        </thead>
        <tbody>
          {#each rows as a (a.label)}
            <tr class:selected={a.label === selected} class:emergency={isEmergency(a)} class:no-position={!hasPosition(a)}
              on:click={() => (selected = selected === a.label ? null : a.label)}
              title={isEmergency(a) ? `Notfall: ${a.emergency}` : hasPosition(a) ? "" : "Keine Position bekannt: nicht auf der Karte"}>
              {#each view.columns as column, i}
                <td class:mono={i === 0}>{column.value(a, station, now)}</td>
              {/each}
            </tr>
          {:else}
            <tr><td colspan={view.columns.length} class="dim">{view.emptyText}</td></tr>
          {/each}
        </tbody>
      </table>
    </div>
  </div>
</div>

<style>
  .traffic {
    flex: 1;
    min-height: 0;
    display: flex;
    flex-direction: column;
    gap: 4px;
  }
  .traffic.fullscreen {
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
