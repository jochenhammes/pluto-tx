<script context="module" lang="ts">
  export type JttyRow = {
    id: string; time: number; direction: "rx" | "tx"; utc: string; snr: number | null; freq: number | null;
    text: string; state: "growing" | "complete" | "expired" | "ended"; gaps: number[];
  };
</script>

<script lang="ts">
  import { afterUpdate } from "svelte";

  // JTTY messages, heard and sent, a row each: the text grows while the frames arrive, unfinished ones are dimmed.
  export let messages: JttyRow[] = [];
  export let connected = false;
  export let droppedS = 0;
  export let error = "";
  export let onClear: () => void = () => {};

  const fmtUtc = (utc: string) => `${utc.slice(0, 2)}:${utc.slice(2, 4)}:${utc.slice(4, 6)}`;
  const STATE_LABELS: Record<string, string> = { growing: "läuft", expired: "abgebrochen", ended: "abgebrochen" };

  let box: HTMLDivElement;
  let follow = true;
  afterUpdate(() => {
    if (box && follow) box.scrollTop = box.scrollHeight;
  });
  function onScroll(): void {
    follow = box.scrollTop + box.clientHeight >= box.scrollHeight - 4;
  }
</script>

<div class="jtty">
  <div class="head">
    <strong>JTTY</strong>
    <span class="dim">{connected ? `${messages.length} Nachrichten` : "Empfänger nicht verbunden"}</span>
    {#if droppedS > 0}<span class="warn">{droppedS} s Audio verloren: Decoder zu langsam</span>{/if}
    {#if error}<span class="warn">{error}</span>{/if}
    <button class="clear" on:click={onClear} disabled={messages.length === 0}>Leeren</button>
  </div>
  <div class="list" bind:this={box} on:scroll={onScroll}>
    {#each messages as m (m.id)}
      <div class="row" class:tx={m.direction === "tx"} class:open={m.state !== "complete"}>
        <span class="dim mono">{fmtUtc(m.utc)}</span>
        <span class="dim mono">{m.direction === "tx" ? "TX" : m.freq === null ? "" : `${Math.round(m.freq)} Hz`}</span>
        <span class="dim mono">{m.snr === null ? "" : `${m.snr} dB`}</span>
        <span class="text mono">{m.text}</span>
        {#if STATE_LABELS[m.state]}<span class="dim">{STATE_LABELS[m.state]}</span>{/if}
      </div>
    {:else}
      <div class="dim">Noch keine Nachrichten.</div>
    {/each}
  </div>
</div>

<style>
  .jtty { display: flex; flex-direction: column; gap: 6px; min-height: 0; height: 100%; }
  .head { display: flex; flex-wrap: wrap; align-items: center; gap: 10px; }
  .clear { margin-left: auto; }
  .warn { color: var(--danger, #c0392b); }
  .list { overflow-y: auto; flex: 1; min-height: 120px; }
  .row { display: grid; grid-template-columns: 5.5em 5em 4em minmax(0, 1fr) auto; gap: 8px; padding: 2px 0; align-items: baseline; }
  .row.open .text { opacity: 0.65; }
  .row.tx .text { font-weight: 600; }
  .text { overflow-wrap: anywhere; }
  .mono { font-family: ui-monospace, monospace; }
  .dim { opacity: 0.7; }
  @media (max-width: 560px) {
    .row { grid-template-columns: 5em 4.5em minmax(0, 1fr); }
    .row > :nth-child(3) { display: none; }
    .row > :nth-child(5) { grid-column: 3; }
  }
</style>
