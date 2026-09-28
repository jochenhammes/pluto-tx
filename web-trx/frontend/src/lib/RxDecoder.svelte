<script context="module" lang="ts">
  export type PocsagCall = { time: string; ric: number; func: number; text: string; baud: number };
  export type RadeStatus = { synced: boolean; snr_db: number; freq_offset_hz: number };
  export type Ft8Decode = { snr_db: number; dt_s: number; freq_hz: number; text: string; sender: string | null };
  export type Ft8Slot = { slot_start: number; utc: string; decodes: Ft8Decode[] };
  export type Ft8Status = { decoder: string | null; slots_decoded: number; last_error: string; clock_synced?: boolean };
</script>

<script lang="ts">
  import { afterUpdate } from "svelte";

  // What the receiver decodes, per active RX mode: RTTY running text, POCSAG
  // calls, the M17 caller, RADE sync/SNR. Fed by App.svelte from the
  // rtty_text / pocsag_message / m17_fields / rade_status events.

  export let mode: string;
  export let connected: boolean;
  export let rttyText = "";
  export let pocsagCalls: PocsagCall[] = [];
  export let m17Caller: { src: string; dst: string; time: string } | null = null;
  export let radeStatus: RadeStatus | null = null;
  export let onClearRtty: () => void = () => {};
  export let ft8Slots: Ft8Slot[] = [];
  export let ft8Status: Ft8Status | null = null;
  export let myCall = "";
  // Click on a decode (F2: take the sender as DX callsign). Optional.
  export let onPickDecode: ((d: Ft8Decode, slot: Ft8Slot) => void) | null = null;

  let ft8OnlyCq = false;
  const isCq = (text: string) => text.startsWith("CQ ");
  const toMe = (text: string) => !!myCall && text.split(" ")[0] === myCall.toUpperCase();
  const fmtUtc = (utc: string) => `${utc.slice(0, 2)}:${utc.slice(2, 4)}:${utc.slice(4, 6)}`;

  let rttyBox: HTMLPreElement;
  let followRtty = true;
  afterUpdate(() => {
    if (rttyBox && followRtty) rttyBox.scrollTop = rttyBox.scrollHeight;
  });
  function onRttyScroll(): void {
    followRtty = rttyBox.scrollTop + rttyBox.clientHeight >= rttyBox.scrollHeight - 4;
  }
</script>

<div class="decoder">
  {#if !connected}
    <div class="dim">Empfänger nicht verbunden.</div>
  {:else if mode === "rtty"}
    <div class="head">
      <span class="dim">RTTY-Empfang</span>
      <button class="small" on:click={onClearRtty}>Leeren</button>
    </div>
    <pre class="rtty" bind:this={rttyBox} on:scroll={onRttyScroll}>{rttyText || " "}</pre>
  {:else if mode === "pocsag"}
    <ul class="calls">
      {#each [...pocsagCalls].reverse() as c}
        <li><span class="dim">{c.time}</span> <b>RIC {c.ric}</b>/{c.func} <span class="dim">{c.baud} Bd</span> {c.text}</li>
      {:else}
        <li class="dim">Noch keine POCSAG-Rufe empfangen.</li>
      {/each}
    </ul>
  {:else if mode === "m17"}
    {#if m17Caller}
      <div class="big">{m17Caller.src} <span class="dim">→</span> {m17Caller.dst}</div>
      <div class="dim">zuletzt {m17Caller.time}</div>
    {:else}
      <div class="dim">Noch kein M17-Signal empfangen.</div>
    {/if}
  {:else if mode === "ft8"}
    <div class="head">
      <span class="dim">
        FT8 · {ft8Status?.decoder ?? "…"} · {ft8Status?.slots_decoded ?? 0} Slots dekodiert
        {#if ft8Status?.last_error}<span class="err">· Fehler: {ft8Status.last_error}</span>{/if}
      </span>
      <label class="check small-check"><input type="checkbox" bind:checked={ft8OnlyCq} /> nur CQ</label>
    </div>
    <div class="ft8">
      <table>
        <thead><tr><th>UTC</th><th>dB</th><th>DT</th><th>Hz</th><th>Nachricht</th></tr></thead>
        {#each [...ft8Slots].reverse() as slot (slot.slot_start)}
          <tbody class="slot">
            {#each slot.decodes.filter((d) => !ft8OnlyCq || isCq(d.text)) as d}
              <tr class:cq={isCq(d.text)} class:me={toMe(d.text)} class:pick={!!onPickDecode && !!d.sender}
                on:click={() => onPickDecode && d.sender && onPickDecode(d, slot)}>
                <td>{fmtUtc(slot.utc)}</td><td>{d.snr_db}</td><td>{d.dt_s.toFixed(1)}</td><td>{d.freq_hz}</td>
                <td class="msg">{d.text}</td>
              </tr>
            {/each}
          </tbody>
        {:else}
          <tbody><tr><td colspan="5" class="dim">Noch keine Decodes — der erste Slot wird ~15 s nach dem Start ausgewertet.</td></tr></tbody>
        {/each}
      </table>
    </div>
  {:else if mode === "rade"}
    {#if radeStatus}
      <div class="big" class:ok={radeStatus.synced}>{radeStatus.synced ? "synchronisiert" : "sucht Signal…"}</div>
      {#if radeStatus.synced}
        <div class="dim">SNR {radeStatus.snr_db} dB · Ablage {radeStatus.freq_offset_hz} Hz</div>
      {/if}
    {:else}
      <div class="dim">Warte auf RADE-Status…</div>
    {/if}
  {:else}
    <div class="dim">Kein Dekoder für diesen Modus — FT8, RTTY, POCSAG, M17 und RADE zeigen hier, was empfangen wird.</div>
  {/if}
</div>

<style>
  .ft8 {
    flex: 1;
    min-height: 0;
    overflow-y: auto;
  }
  .ft8 table {
    width: 100%;
    border-collapse: collapse;
    font-family: var(--mono);
    font-size: 0.78rem;
  }
  .ft8 th {
    position: sticky;
    top: 0;
    background: var(--bg-1);
    text-align: left;
    color: var(--text-dim);
    font-weight: 500;
  }
  .ft8 td,
  .ft8 th {
    padding: 1px 6px;
    white-space: nowrap;
  }
  .ft8 td:nth-child(2),
  .ft8 td:nth-child(3),
  .ft8 td:nth-child(4) {
    text-align: right;
  }
  /* Number columns only as wide as their content; the message takes the rest. */
  .ft8 th:not(:last-child),
  .ft8 td:not(.msg) {
    width: 1%;
  }
  .ft8 th:nth-child(2),
  .ft8 th:nth-child(3),
  .ft8 th:nth-child(4) {
    text-align: right;
  }
  .ft8 .msg,
  .ft8 th:last-child {
    padding-left: 16px;
  }
  .ft8 tbody.slot {
    border-top: 1px solid var(--border);
  }
  .ft8 tr.cq .msg {
    color: var(--ok);
  }
  .ft8 tr.me {
    background: #5c3d0f;
  }
  .ft8 tr.pick {
    cursor: pointer;
  }
  .ft8 tr.pick:hover {
    background: var(--bg-3);
  }
  .err {
    color: var(--danger);
  }
  .small-check {
    font-size: 0.72rem;
  }
  .decoder {
    flex: 1;
    min-height: 0;
    display: flex;
    flex-direction: column;
    gap: 4px;
  }
  .head {
    display: flex;
    justify-content: space-between;
    align-items: center;
  }
  .rtty {
    flex: 1;
    min-height: 0;
    overflow-y: auto;
    margin: 0;
    padding: 6px 8px;
    background: var(--bg-0);
    border: 1px solid var(--border);
    border-radius: var(--radius);
    font-family: var(--mono);
    font-size: 0.85rem;
    white-space: pre-wrap;
    word-break: break-all;
  }
  .calls {
    flex: 1;
    min-height: 0;
    overflow-y: auto;
    list-style: none;
    padding: 0;
    margin: 0;
    font-size: 0.8rem;
  }
  .calls li {
    padding: 3px 0;
    border-bottom: 1px solid var(--border);
  }
  .big {
    font-size: 1.2rem;
    font-weight: 600;
  }
  .big.ok {
    color: var(--ok);
  }
  .dim {
    color: var(--text-dim);
    font-size: 0.75rem;
  }
  button.small {
    font-size: 0.72rem;
    padding: 2px 8px;
  }
</style>
