<script lang="ts">
  import { tick } from "svelte";

  // Frequency display/entry in the style of SDR++: "145.150.000" Hz with
  // dots as thousands separators. Mouse wheel over a digit steps that
  // digit; a click switches to free text entry. Every change is committed
  // straight away via onCommit (i.e. it tunes), there is no separate
  // "Tune" button.
  export let value: number; // Hz
  export let onCommit: (hz: number) => void;
  export let disabled = false;
  export let size: "large" | "normal" = "normal";

  const DIGITS = 10; // up to 9.999.999.999 Hz

  let editing = false;
  let text = "";
  let inputEl: HTMLInputElement;

  $: digits = String(Math.max(0, Math.round(value))).padStart(DIGITS, "0").split("");
  $: firstSignificant = digits.findIndex((d) => d !== "0");

  export function formatHz(hz: number): string {
    return Math.round(hz).toString().replace(/\B(?=(\d{3})+(?!\d))/g, ".");
  }

  /** Accepts "145.150.000" / "145150000" (Hz), "145,15" / "145.15" /
   * "145" (MHz). Dots are thousands separators when there is more than
   * one; a single dot or a comma is a MHz decimal point. */
  function parse(input: string): number | null {
    const t = input.trim().replace(/\s|hz/gi, "");
    if (!t) return null;
    let hz: number;
    const dots = (t.match(/\./g) || []).length;
    if (t.includes(",")) hz = parseFloat(t.replace(/\./g, "").replace(",", ".")) * 1e6;
    else if (dots >= 2) hz = parseInt(t.replace(/\./g, ""), 10);
    else if (dots === 1) hz = parseFloat(t) * 1e6;
    else {
      const n = parseInt(t, 10);
      hz = n < 100_000 ? n * 1e6 : n;
    }
    return Number.isFinite(hz) && hz > 0 ? Math.round(hz) : null;
  }

  function commit(hz: number): void {
    value = hz;
    onCommit(hz);
  }

  function onWheel(e: WheelEvent, index: number): void {
    if (disabled) return;
    e.preventDefault();
    const step = 10 ** (DIGITS - 1 - index);
    const next = value + (e.deltaY < 0 ? step : -step);
    if (next > 0) commit(next);
  }

  async function startEdit(): Promise<void> {
    if (disabled) return;
    text = formatHz(value);
    editing = true;
    await tick();
    inputEl.select();
  }

  function finishEdit(apply: boolean): void {
    if (!editing) return;
    editing = false;
    if (!apply) return;
    const hz = parse(text);
    if (hz !== null && hz !== value) commit(hz);
  }

  function onKey(e: KeyboardEvent): void {
    if (e.key === "Enter") finishEdit(true);
    else if (e.key === "Escape") finishEdit(false);
  }
</script>

{#if editing}
  <input
    bind:this={inputEl}
    bind:value={text}
    class="freq-edit {size}"
    type="text"
    inputmode="decimal"
    on:keydown={onKey}
    on:blur={() => finishEdit(true)}
  />
{:else}
  <!-- svelte-ignore a11y-click-events-have-key-events -->
  <div
    class="freq {size}"
    class:disabled
    role="button"
    tabindex="0"
    title="Mausrad über einer Ziffer: ändern · Klick: eingeben"
    on:click={startEdit}
    on:keydown={(e) => e.key === "Enter" && startEdit()}
  >
    {#each digits as d, i}
      {#if i > 0 && (DIGITS - i) % 3 === 0}<span class="sep" class:lead={i <= firstSignificant}>.</span>{/if}
      <span class="digit" class:lead={i < firstSignificant} on:wheel={(e) => onWheel(e, i)}>{d}</span>
    {/each}
    <span class="unit">Hz</span>
  </div>
{/if}

<style>
  .freq,
  .freq-edit {
    font-family: var(--mono);
    font-variant-numeric: tabular-nums;
    background: var(--bg-2);
    border: 1px solid var(--border);
    border-radius: var(--radius);
    padding: 4px 10px;
    color: var(--text);
    font-size: 1.25rem;
    line-height: 1.4;
    min-width: 0;
  }
  .freq.large,
  .freq-edit.large {
    font-size: 1.9rem;
  }
  .freq {
    display: inline-flex;
    align-items: baseline;
    cursor: text;
    user-select: none;
  }
  .freq.disabled {
    opacity: 0.55;
    cursor: default;
  }
  .freq-edit {
    width: 100%;
  }
  .digit {
    padding: 0 0.02em;
    border-radius: 3px;
    cursor: ns-resize;
  }
  .freq:not(.disabled) .digit:hover {
    background: var(--accent-dim);
  }
  .lead {
    color: #3a4257;
  }
  .sep {
    color: var(--text-dim);
  }
  .unit {
    margin-left: 0.5em;
    font-size: 0.55em;
    color: var(--text-dim);
  }
</style>
