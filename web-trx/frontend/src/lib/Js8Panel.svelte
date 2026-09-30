<script context="module" lang="ts">
  // Events from the backend (web_trx/radio_backend.py _on_js8_decodes, js8_series.py).
  export type Js8FrameDecode = { snr_db: number; dt_s: number; freq_hz: number; text: string; frame: string; flags: number };
  export type Js8Period = { slot_start: number; utc: string; speed: string; decodes: Js8FrameDecode[] };
  export type Js8Message = {
    id: number; speed: string; utc: string; first_slot: number; last_slot: number; freq_hz: number; snr_db: number;
    sender: string; to: string; text: string; closed: boolean; complete: boolean; incomplete: boolean;
  };
  export type Js8Status = { decoder: string | null; periods_decoded: number; last_error: string; clock_synced?: boolean;
                            speeds?: string[] };
  // JS8 automation (web_trx/js8_automation.py, pluto-tx J9)
  export type Js8AutoConfig = { autoreply: boolean; confirm: boolean; hb_mode: boolean; hb_interval_min: number;
                                hb_ack: boolean; relay: boolean; info: string; status: string; idle_watchdog_min: number };
  export type Js8AutoStatus = {
    config: Js8AutoConfig; watchdog: boolean; idle_s: number; auto_last_hour: number; auto_max_per_hour: number;
    hb_next: number | null; queue: { id: number; text: string; priority: number }[];
    pending: { id: number; text: string; expires_in_s: number }[]; log: { utc: string; text: string }[];
  };
  export type Js8AutoEvent = { type: string; utc: string; id?: number; text?: string; reason?: string; [k: string]: unknown };
  export type Js8InboxMsg = { id: number; type: string; utc: string; from: string; to: string; path: string; text: string;
                              snr_db: number | null };
</script>

<script lang="ts">
  import { afterUpdate } from "svelte";

  // JS8 receive view: conversations (chat), band activity (latest frame per audio offset) and heard
  // stations. A click on a station or message takes its callsign into the TX "To" field (onPick).
  export let messages: Js8Message[] = [];
  export let periods: Js8Period[] = [];
  export let status: Js8Status | null = null;
  export let myCall = "";
  export let onPick: ((call: string, freqHz: number) => void) | null = null;
  // Automation: null = not available (no TX codec / backend without it)
  export let auto: Js8AutoStatus | null = null;
  export let autoEvents: Js8AutoEvent[] = [];
  export let inbox: Js8InboxMsg[] = [];
  export let txReady = false;
  export let onAuto: ((action: string, params?: Record<string, unknown>) => void) | null = null;
  export let onUseText: ((text: string) => void) | null = null;

  type Tab = "chat" | "band" | "stations" | "auto" | "inbox";
  let tab: Tab = "chat";
  let conversation = "*";              // "*" = everything, else a callsign or group
  const ALL = "*";

  const fmtUtc = (utc: string) => `${utc.slice(0, 2)}:${utc.slice(2, 4)}:${utc.slice(4, 6)}`;
  const isCall = (s: string) => !!s && !s.startsWith("@") && s !== "<....>";
  $: me = myCall.toUpperCase();

  function counterpart(m: Js8Message): string {
    if (m.to.startsWith("@")) return m.to;                         // @ALLCALL, @HB, groups
    if (me && m.sender === me) return m.to || ALL;
    if (me && m.to === me) return m.sender || ALL;
    return ALL;
  }
  $: conversations = [ALL, ...Array.from(new Set(messages.map(counterpart).filter((c) => c !== ALL)))];
  $: shown = conversation === ALL ? messages : messages.filter((m) => counterpart(m) === conversation);

  // Band activity: the newest frame per 10 Hz offset bucket
  $: activity = (() => {
    const latest = new Map<number, { p: Js8Period; d: Js8FrameDecode }>();
    for (const p of periods) for (const d of p.decodes) latest.set(Math.round(d.freq_hz / 10) * 10, { p, d });
    return [...latest.entries()].sort((a, b) => a[0] - b[0]).map(([, v]) => v);
  })();
  const senderOf = (text: string) => (text.includes(":") ? text.split(":")[0].trim() : "");

  // Stations: from assembled messages (sender) -- grid from CQ/heartbeat texts
  $: stations = (() => {
    const map = new Map<string, { call: string; grid: string; snr: number; freq: number; last: number; text: string }>();
    for (const m of messages) {
      if (!isCall(m.sender)) continue;
      const prev = map.get(m.sender);
      const grid = /(?:HEARTBEAT|CQ)\s.*?\b([A-R]{2}[0-9]{2})\s*$/.exec(m.text)?.[1] ?? prev?.grid ?? "";
      if (!prev || m.last_slot >= prev.last) {
        map.set(m.sender, { call: m.sender, grid, snr: m.snr_db, freq: m.freq_hz, last: m.last_slot, text: m.text });
      }
    }
    return [...map.values()].sort((a, b) => b.last - a.last);
  })();
  const fmtEpoch = (s: number) => new Date(s * 1000).toISOString().slice(11, 19);

  function pick(call: string, freq: number): void {
    if (onPick && isCall(call) && call !== me) onPick(call, freq);
  }

  // --- automation ---
  function setCfg(name: keyof Js8AutoConfig, value: unknown): void {
    onAuto?.("config", { config: { [name]: value } });
  }
  const checked = (e: Event) => (e.currentTarget as HTMLInputElement).checked;
  const value = (e: Event) => (e.currentTarget as HTMLInputElement | HTMLSelectElement).value;
  $: suggestions = autoEvents.filter((e) => e.type === "suggest").slice(-5).reverse();
  $: pendingById = new Map((auto?.pending ?? []).map((p) => [p.id, p]));
  $: unread = inbox.filter((m) => m.type === "UNREAD").length;
  function openInbox(): void {
    tab = "inbox";
    onAuto?.("inbox");
  }
  const HB_CHOICES = [0, 10, 15, 30, 60];   // JS8Call's heartbeat repeat menu (buildRepeatMenu)

  let chatBox: HTMLDivElement;
  let follow = true;
  afterUpdate(() => {
    if (chatBox && follow) chatBox.scrollTop = chatBox.scrollHeight;
  });
  function onChatScroll(): void {
    follow = chatBox.scrollTop + chatBox.clientHeight >= chatBox.scrollHeight - 4;
  }
</script>

<div class="js8">
  <div class="head">
    <span class="dim">
      JS8 · {status?.decoder ?? "…"} · {status?.periods_decoded ?? 0} Perioden
      {#if status?.speeds?.length}· {status.speeds.join(", ")}{/if}
      {#if status?.last_error}<span class="err">· Fehler: {status.last_error}</span>{/if}
    </span>
    <div class="tabs">
      <button class="small" class:on={tab === "chat"} on:click={() => (tab = "chat")}>Chat</button>
      <button class="small" class:on={tab === "band"} on:click={() => (tab = "band")}>Band</button>
      <button class="small" class:on={tab === "stations"} on:click={() => (tab = "stations")}>Stationen ({stations.length})</button>
      {#if auto && onAuto}
        <button class="small" class:on={tab === "auto"} class:alert={!!auto.pending.length || auto.watchdog}
          on:click={() => (tab = "auto")}>Automatik{#if auto.config.autoreply || auto.config.hb_mode} ●{/if}</button>
        <button class="small" class:on={tab === "inbox"} on:click={openInbox}>Inbox{#if unread} ({unread}){/if}</button>
      {/if}
    </div>
  </div>

  {#if tab === "chat"}
    <div class="convs">
      {#each conversations as c}
        <button class="chip" class:on={conversation === c} on:click={() => (conversation = c)}>{c === ALL ? "Alle" : c}</button>
      {/each}
    </div>
    <div class="chat" bind:this={chatBox} on:scroll={onChatScroll}>
      {#each shown as m (m.id)}
        <div class="msg" class:mine={!!me && m.sender === me} class:tome={!!me && m.to === me}
          class:open={!m.closed} class:pick={!!onPick && isCall(m.sender) && m.sender !== me}
          on:click={() => pick(m.sender, m.freq_hz)} on:keydown={() => {}} role="button" tabindex="-1">
          <span class="dim">{fmtUtc(m.utc)} · {m.freq_hz} Hz · {m.snr_db} dB · {m.speed}</span>
          <div class="text">{m.text}{#if !m.closed}<span class="dim"> ▸</span>{/if}</div>
        </div>
      {:else}
        <div class="dim">Noch keine JS8-Nachrichten — jede Periode wird nach ihrem Ende ausgewertet.</div>
      {/each}
    </div>
  {:else if tab === "band"}
    <div class="table-wrap">
      <table>
        <thead><tr><th>Hz</th><th>UTC</th><th>dB</th><th>Speed</th><th>Letzter Rahmen</th></tr></thead>
        <tbody>
          {#each activity as { p, d }}
            <tr class:pick={!!onPick && isCall(senderOf(d.text))} on:click={() => pick(senderOf(d.text), d.freq_hz)}>
              <td>{d.freq_hz}</td><td>{fmtUtc(p.utc)}</td><td>{d.snr_db}</td><td>{p.speed}</td><td class="txt">{d.text}</td>
            </tr>
          {:else}
            <tr><td colspan="5" class="dim">Noch keine Aktivität.</td></tr>
          {/each}
        </tbody>
      </table>
    </div>
  {:else if tab === "auto" && auto}
    <div class="auto">
      <p class="dim warn">
        Automatische Aussendungen (JS8Call-Regeln): alles startet aus. Gesendet wird nur mit verbundenem TX im
        JS8-Modus, über dieselbe Kette wie von Hand (Band, Leistungsdeckel, NOTAUS). Ohne Bedienung für
        {auto.config.idle_watchdog_min} min oder ohne verbundenen Browser schaltet sich alles ab. Höchstens
        {auto.auto_max_per_hour} automatische Aussendungen pro Stunde.
      </p>
      {#if auto.watchdog}
        <p class="err">Watchdog ausgelöst — Automatik aus. Jede Bedienung hebt ihn auf; die Schalter bleiben aus.</p>
      {/if}
      {#if !txReady}<p class="dim">TX nicht im JS8-Modus verbunden: Antworten bleiben in der Warteschlange.</p>{/if}
      <div class="grid">
        <label><input type="checkbox" checked={auto.config.autoreply}
          on:change={(e) => setCfg("autoreply", checked(e))} /> Autoreply</label>
        <label><input type="checkbox" checked={auto.config.confirm}
          on:change={(e) => setCfg("confirm", checked(e))} /> Vor dem Senden bestätigen (90 s)</label>
        <label><input type="checkbox" checked={auto.config.hb_mode}
          on:change={(e) => setCfg("hb_mode", checked(e))} /> Heartbeat-Modus</label>
        <label>Heartbeat alle
          <select value={String(auto.config.hb_interval_min)} on:change={(e) => setCfg("hb_interval_min", Number(value(e)))}>
            {#each HB_CHOICES as m}<option value={String(m)}>{m ? `${m} min` : "aus"}</option>{/each}
            {#if !HB_CHOICES.includes(auto.config.hb_interval_min)}
              <option value={String(auto.config.hb_interval_min)}>{auto.config.hb_interval_min} min</option>
            {/if}
          </select></label>
        <label><input type="checkbox" checked={auto.config.hb_ack}
          on:change={(e) => setCfg("hb_ack", checked(e))} /> Heartbeats beantworten (HB-ACK)</label>
        <label><input type="checkbox" checked={auto.config.relay}
          on:change={(e) => setCfg("relay", checked(e))} /> Relay / Nachrichten für andere speichern</label>
        <label>INFO <input type="text" value={auto.config.info} maxlength="60" placeholder="leer = keine Antwort auf INFO?"
          on:change={(e) => setCfg("info", value(e).toUpperCase())} /></label>
        <label>Idle-Watchdog <input type="number" min="1" max="60" value={auto.config.idle_watchdog_min}
          on:change={(e) => setCfg("idle_watchdog_min", Number(value(e)))} /> min</label>
      </div>
      <div class="row">
        <button class="small" disabled={!txReady} on:click={() => onAuto?.("heartbeat_now")}>Heartbeat jetzt</button>
        <button class="small" disabled={!auto.queue.length && !auto.pending.length}
          on:click={() => onAuto?.("clear_queue")}>Warteschlange leeren</button>
        <span class="dim">
          {auto.auto_last_hour}/{auto.auto_max_per_hour} automatisch in der letzten Stunde
          {#if auto.hb_next}· nächster Heartbeat {fmtEpoch(auto.hb_next)} UTC{/if}
        </span>
      </div>
      {#each auto.pending as p (p.id)}
        <div class="confirm">
          <span>Senden? <b>{p.text}</b> <span class="dim">({p.expires_in_s} s)</span></span>
          <button class="small" on:click={() => onAuto?.("confirm", { id: p.id, yes: true })}>Ja</button>
          <button class="small" on:click={() => onAuto?.("confirm", { id: p.id, yes: false })}>Nein</button>
        </div>
      {/each}
      {#each auto.queue as q (q.id)}
        <div class="dim">In der Warteschlange: {q.text}</div>
      {/each}
      {#each suggestions as sgt (sgt.id)}
        {#if !pendingById.has(Number(sgt.id))}
          <div class="suggest">
            <span class="dim">Antwortvorschlag (Autoreply aus):</span> <b>{sgt.text}</b>
            {#if onUseText}<button class="small" on:click={() => onUseText?.(String(sgt.text))}>Ins Sendefeld</button>{/if}
          </div>
        {/if}
      {/each}
      <div class="log">
        {#each [...auto.log].reverse() as l}<div><span class="dim">{l.utc}</span> {l.text}</div>{/each}
      </div>
    </div>
  {:else if tab === "inbox"}
    <div class="table-wrap">
      <table>
        <thead><tr><th>Typ</th><th>UTC</th><th>Von</th><th>An</th><th>Text</th><th></th></tr></thead>
        <tbody>
          {#each inbox as m (m.id)}
            <tr>
              <td>{m.type === "UNREAD" ? "an mich" : m.type === "STORE" ? "gespeichert" : m.type === "DELIVERED" ? "abgeholt" : m.type}</td>
              <td>{m.utc}</td><td><b>{m.from}</b>{#if m.path && m.path !== m.from}<span class="dim"> ({m.path})</span>{/if}</td>
              <td>{m.to}</td><td class="txt">{m.text}</td>
              <td><button class="small" on:click={() => onAuto?.("inbox_delete", { id: m.id })}>Löschen</button></td>
            </tr>
          {:else}
            <tr><td colspan="6" class="dim">Keine Nachrichten.</td></tr>
          {/each}
        </tbody>
      </table>
    </div>
  {:else}
    <div class="table-wrap">
      <table>
        <thead><tr><th>Call</th><th>Grid</th><th>dB</th><th>Hz</th><th>UTC</th><th>Zuletzt</th></tr></thead>
        <tbody>
          {#each stations as s (s.call)}
            <tr class:pick={!!onPick && s.call !== me} on:click={() => pick(s.call, s.freq)}>
              <td><b>{s.call}</b></td><td>{s.grid}</td><td>{s.snr}</td><td>{s.freq}</td><td>{fmtEpoch(s.last)}</td>
              <td class="txt">{s.text}</td>
            </tr>
          {:else}
            <tr><td colspan="6" class="dim">Noch keine Stationen gehört.</td></tr>
          {/each}
        </tbody>
      </table>
    </div>
  {/if}
</div>

<style>
  .js8 {
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
    gap: 8px;
  }
  .tabs {
    display: flex;
    gap: 4px;
  }
  button.small {
    font-size: 0.72rem;
    padding: 2px 8px;
  }
  button.on {
    background: var(--bg-3);
    border-color: var(--accent, #3ea6ff);
  }
  .convs {
    display: flex;
    flex-wrap: wrap;
    gap: 4px;
  }
  .chip {
    font-size: 0.72rem;
    padding: 1px 8px;
    border-radius: 999px;
    font-family: var(--mono);
  }
  .chat {
    flex: 1;
    min-height: 0;
    overflow-y: auto;
    display: flex;
    flex-direction: column;
    gap: 3px;
  }
  .msg {
    padding: 2px 6px;
    border-left: 3px solid transparent;
    font-size: 0.8rem;
  }
  .msg .text {
    font-family: var(--mono);
    white-space: pre-wrap;
    word-break: break-word;
  }
  .msg.mine {
    border-left-color: var(--warn, #f5c211);
  }
  .msg.tome {
    background: #5c3d0f;
  }
  .msg.open .text {
    color: var(--text-dim);
  }
  .pick {
    cursor: pointer;
  }
  .pick:hover {
    background: var(--bg-3);
  }
  .table-wrap {
    flex: 1;
    min-height: 0;
    overflow-y: auto;
  }
  table {
    width: 100%;
    border-collapse: collapse;
    font-family: var(--mono);
    font-size: 0.78rem;
  }
  th {
    position: sticky;
    top: 0;
    background: var(--bg-1);
    text-align: left;
    color: var(--text-dim);
    font-weight: 500;
  }
  td,
  th {
    padding: 1px 6px;
    white-space: nowrap;
  }
  td:not(.txt),
  th:not(:last-child) {
    width: 1%;
  }
  .txt {
    white-space: pre-wrap;
    word-break: break-word;
  }
  .err {
    color: var(--danger);
  }
  button.alert {
    border-color: var(--warn, #f5c211);
  }
  .auto {
    flex: 1;
    min-height: 0;
    overflow-y: auto;
    display: flex;
    flex-direction: column;
    gap: 6px;
    font-size: 0.8rem;
  }
  .auto .grid {
    display: grid;
    grid-template-columns: repeat(auto-fill, minmax(260px, 1fr));
    gap: 4px 12px;
  }
  .auto .row {
    display: flex;
    gap: 8px;
    align-items: center;
    flex-wrap: wrap;
  }
  .auto input[type="text"] {
    width: 14em;
  }
  .auto input[type="number"] {
    width: 4em;
  }
  .confirm,
  .suggest {
    display: flex;
    gap: 8px;
    align-items: center;
    flex-wrap: wrap;
    font-family: var(--mono);
    padding: 2px 6px;
    border-left: 3px solid var(--warn, #f5c211);
  }
  .log {
    font-family: var(--mono);
    font-size: 0.75rem;
  }
  p {
    margin: 0;
  }
  .warn {
    border-left: 3px solid var(--warn, #f5c211);
    padding-left: 6px;
  }
  .dim {
    color: var(--text-dim);
    font-size: 0.75rem;
  }
</style>
