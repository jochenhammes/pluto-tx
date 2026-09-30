<script lang="ts">
  // Where voice-mode TX audio comes from (docs/BROWSER_AUDIO.md): an input device of this computer or an
  // audio file. App.svelte owns the TxAudioInput and the PTT; this panel only edits the choice.
  export let kind: "device" | "file" = "device";
  export let devices: { deviceId: string; label: string }[] = [];
  export let deviceId = "";
  export let processing = true;
  export let gainDb = 0;
  export let loop = false;
  export let fileName = "";
  export let fileDurationS = 0;
  export let fileError = "";
  export let levelDb = -90;
  export let monitor = false;
  export let locked = false;                        // PTT held: the source can't change mid-over
  export let onGrant: () => void = () => {};
  export let onFile: (f: File) => void = () => {};
  export let onChange: () => void = () => {};
  export let onDeviceChange: () => void = () => {};
  export let onToggleMonitor: () => void = () => {};

  $: named = devices.some((d) => !/^Eingang \d+$/.test(d.label));
  const fmtDur = (s: number) => `${Math.floor(s / 60)}:${String(Math.round(s % 60)).padStart(2, "0")}`;

  function pickFile(e: Event): void {
    const f = (e.currentTarget as HTMLInputElement).files?.[0];
    if (f) onFile(f);
  }
</script>

<div class="subpanel txsrc">
  <div class="subpanel-title">
    Audioquelle
    {#if kind === "device"}
      <button class="small" on:click={onToggleMonitor}>{monitor ? "Pegel aus" : "Pegel anzeigen"}</button>
    {/if}
  </div>
  <div class="row">
    <label><input type="radio" value="device" bind:group={kind} disabled={locked} on:change={onChange} /> Eingang</label>
    <label><input type="radio" value="file" bind:group={kind} disabled={locked} on:change={onChange} /> Datei</label>
  </div>

  {#if kind === "device"}
    <div class="row">
      <select id="txAudioDevice" bind:value={deviceId} disabled={locked} on:change={onDeviceChange}>
        <option value="">Standard-Eingang</option>
        {#each devices as d (d.deviceId)}{#if d.deviceId && d.deviceId !== "default"}<option value={d.deviceId}>{d.label}</option>{/if}{/each}
      </select>
      {#if !named}<button class="small" on:click={onGrant}>Geräte freigeben</button>{/if}
    </div>
    <label class="dim" title="Echounterdrückung, Rauschunterdrückung und automatische Pegelregelung des Browsers">
      <input id="txAudioProcessing" type="checkbox" bind:checked={processing} disabled={locked} on:change={onChange} />
      Sprachverarbeitung des Browsers (für Mikrofone; bei Line/Soundkarte aus)
    </label>
  {:else}
    <div class="row">
      <input id="txAudioFile" type="file" accept="audio/*" disabled={locked} on:change={pickFile} />
    </div>
    {#if fileName}<div class="dim">{fileName} · {fmtDur(fileDurationS)}</div>{/if}
    {#if fileError}<div class="err">{fileError}</div>{/if}
    <label class="dim"><input type="checkbox" bind:checked={loop} disabled={locked} on:change={onChange} />
      Schleife (sonst endet die Aussendung am Dateiende)</label>
  {/if}

  <label class="dim gain">Eingangspegel {gainDb > 0 ? "+" : ""}{gainDb} dB
    <input id="txAudioGain" type="range" min="-30" max="20" step="1" bind:value={gainDb} on:input={onChange} />
  </label>
  <div class="meter" title="Spitzenpegel der TX-Audioquelle (dBFS)">
    <div class="meter-fill" class:hot={levelDb > -3} style="width: {Math.max(0, Math.min(100, (levelDb + 60) / 60 * 100))}%"></div>
  </div>
  <div class="dim">{levelDb > -90 ? `${levelDb.toFixed(1)} dBFS` : "—"}</div>
</div>

<style>
  /* the same look as App.svelte's .subpanel / .meter (Svelte scopes styles per component) */
  .subpanel {
    border: 1px solid var(--border);
    border-radius: var(--radius);
    padding: 10px 12px;
    background: var(--bg-0);
    margin-bottom: 12px;
  }
  .subpanel-title {
    display: flex;
    align-items: center;
    justify-content: space-between;
    font-size: 0.72rem;
    text-transform: uppercase;
    letter-spacing: 0.06em;
    color: var(--text-dim);
    margin-bottom: 8px;
  }
  button.small {
    font-size: 0.72rem;
    padding: 2px 8px;
  }
  .meter {
    height: 10px;
    background: var(--bg-2);
    border-radius: 5px;
    overflow: hidden;
    margin: 4px 0;
  }
  .meter-fill {
    height: 100%;
    background: linear-gradient(90deg, #1f8a4c, #33d17a 70%, #f5c211);
    transition: width 60ms linear;
  }
  .meter-fill.hot {
    background: var(--danger);
  }
  .dim {
    color: var(--text-dim);
    font-size: 0.78rem;
  }
  .txsrc label {
    font-size: 0.8rem;
    display: block;
    margin: 4px 0;
  }
  .txsrc .row label {
    display: inline;
  }
  .txsrc .row {
    display: flex;
    gap: 10px;
    align-items: center;
    flex-wrap: wrap;
    margin: 3px 0;
  }
  .txsrc select {
    max-width: 22em;
  }
  .txsrc .gain {
    display: flex;
    gap: 8px;
    align-items: center;
  }
  .gain input {
    flex: 1;
  }
  .err {
    color: var(--danger);
    font-size: 0.8rem;
  }
</style>
