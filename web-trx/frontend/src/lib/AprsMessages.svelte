<script context="module" lang="ts">
  export type AprsMessage = {
    direction: "rx" | "tx"; time_ms: number; source: string; addressee: string; text: string; message_id: string; acked?: boolean;
  };
</script>

<script lang="ts">
  // APRS text messages, heard and sent. An acknowledgement marks the sent message it answers.
  export let messages: AprsMessage[] = [];
  export let myCall = "";
  export let notice = "";
  export let autoAck = false;
  export let canAutoAck = false;
  export let onAutoAck: (enabled: boolean) => void = () => {};

  const clock = (ms: number): string => new Date(ms).toLocaleTimeString("de-DE");
</script>

<div class="aprs-messages">
  <div class="head">
    <strong>Nachrichten</strong>
    <label class="check" title="Beantwortet Nachrichten an {myCall || 'das eigene Rufzeichen'} automatisch. Sendet dann ohne Klick, nur wenn der Sender auf APRS und auf der Empfangsfrequenz steht. Geht aus, wenn kein Browser mehr verbunden ist.">
      <input type="checkbox" checked={autoAck} disabled={!canAutoAck}
        on:change={(e) => onAutoAck(e.currentTarget.checked)} /> Automatisch bestätigen
    </label>
    {#if notice}<span class="notice">{notice}</span>{/if}
  </div>
  <div class="list">
    {#each [...messages].reverse() as m}
      <div class="row" class:mine={m.addressee === myCall && !!myCall}>
        <span class="dim">{clock(m.time_ms)}</span>
        <span class="mono">{m.source} → {m.addressee}</span>
        <span class="text">{m.text}</span>
        {#if m.direction === "tx"}<span class="dim">{m.acked ? "bestätigt" : m.message_id ? "unbestätigt" : ""}</span>{/if}
      </div>
    {:else}
      <div class="dim">Noch keine Nachrichten.</div>
    {/each}
  </div>
</div>

<style>
  .aprs-messages {
    flex: 1 1 0;
    min-height: 0;
    display: flex;
    flex-direction: column;
    gap: 2px;
    border-top: 1px solid var(--border, #333);
    padding-top: 2px;
    font-size: 0.78rem;
  }
  .head {
    display: flex;
    flex-wrap: wrap;
    gap: 4px 12px;
    align-items: center;
  }
  .notice {
    color: var(--text-dim);
  }
  .list {
    flex: 1;
    min-height: 0;
    overflow-y: auto;
  }
  .row {
    display: flex;
    flex-wrap: wrap;
    gap: 2px 8px;
    padding: 1px 0;
  }
  .row.mine .text {
    font-weight: 600;
  }
  .mono {
    font-family: var(--mono);
  }
  .text {
    overflow-wrap: anywhere;
  }
  .dim {
    color: var(--text-dim);
  }
</style>
