<script context="module" lang="ts">
  export type MeshcoreMessage = {
    direction: "rx" | "tx"; time_ms: number; kind: string; channel: string; sender: string; text: string; verified: boolean;
  };
  export type MeshcoreChannel = { name: string; hash: number };
</script>

<script lang="ts">
  // MeshCore messages, heard and sent, and what belongs to reading them: the channel keys and the options. A saved
  // key never comes back from the server: the list shows names only, and the field empties after saving.
  export let messages: MeshcoreMessage[] = [];
  export let channels: MeshcoreChannel[] = [];
  export let decryptDm = false;
  export let showUnverified = false;
  export let publicKey = "";
  export let onSaveChannel: (name: string, secret: string) => void = () => {};
  export let onDeleteChannel: (name: string) => void = () => {};
  export let onOptions: (options: { decrypt_dm?: boolean; show_unverified?: boolean }) => void = () => {};

  let channelName = "";
  let channelSecret = "";
  let managing = false;

  const clock = (ms: number): string => new Date(ms).toLocaleTimeString("de-DE");
  const label = (m: MeshcoreMessage): string =>
    m.kind === "group_text" ? `# ${m.channel}` : m.kind === "direct_text" ? "Direkt" : m.kind;
  function save(): void {
    onSaveChannel(channelName, channelSecret);
    channelName = "";
    channelSecret = "";
  }
</script>

<div class="mc-messages">
  <div class="head">
    <strong>Nachrichten</strong>
    <label class="check" title="Entschlüsselt Nachrichten an die eigene Node, sobald ihr Absender per Advert bekannt ist. Legt beim ersten Einschalten die Node-Identität an.">
      <input type="checkbox" checked={decryptDm} on:change={(e) => onOptions({ decrypt_dm: e.currentTarget.checked })} /> Direktnachrichten
    </label>
    <label class="check" title="Zeigt auch Pakete, deren Inhalt sich nicht prüfen lässt, etwa Kanäle ohne Schlüssel.">
      <input type="checkbox" checked={showUnverified} on:change={(e) => onOptions({ show_unverified: e.currentTarget.checked })} /> Ungeprüfte
    </label>
    <button class="small" on:click={() => (managing = !managing)}>Kanäle ({channels.length})</button>
  </div>
  {#if managing}
    <div class="channels">
      {#each channels as c (c.name)}
        <span class="channel"><span class="mono">{c.name}</span> <button class="small" on:click={() => onDeleteChannel(c.name)}>Löschen</button></span>
      {/each}
      <form class="add" autocomplete="off" on:submit|preventDefault={save}>
        <label for="mcChannelName">Name</label>
        <input id="mcChannelName" type="text" maxlength="32" autocomplete="off" bind:value={channelName} />
        <label for="mcChannelSecret">Schlüssel</label>
        <input id="mcChannelSecret" class="mono" type="password" maxlength="32" autocomplete="new-password" placeholder="32 Hex-Zeichen" bind:value={channelSecret} />
        <button class="small" type="submit" disabled={!channelName.trim() || !channelSecret}>Speichern</button>
      </form>
      <span class="dim">Der Schlüssel bleibt auf dem Server und wird hier nicht wieder angezeigt.</span>
      {#if publicKey}<span class="dim key">Eigene Node: <span class="mono">{publicKey}</span></span>{/if}
    </div>
  {/if}
  <div class="list">
    {#each [...messages].reverse() as m}
      <div class="row">
        <span class="dim">{clock(m.time_ms)}</span>
        <span class="mono">{label(m)}</span>
        <span class="mono">{m.direction === "tx" ? "→" : ""} {m.sender}</span>
        <span class="text">{m.text}</span>
        {#if !m.verified}<span class="dim">nicht geprüft</span>{/if}
      </div>
    {:else}
      <div class="dim">Noch keine Nachrichten.</div>
    {/each}
  </div>
</div>

<style>
  .mc-messages {
    flex: 1 1 0;
    min-height: 0;
    display: flex;
    flex-direction: column;
    gap: 2px;
    border-top: 1px solid var(--border, #333);
    padding-top: 2px;
    font-size: 0.78rem;
  }
  .head,
  .channels,
  .add {
    display: flex;
    flex-wrap: wrap;
    gap: 4px 12px;
    align-items: center;
  }
  .channels {
    padding: 4px 0;
  }
  .add input {
    width: 11em;
    min-width: 0;
  }
  .key {
    overflow-wrap: anywhere;
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
