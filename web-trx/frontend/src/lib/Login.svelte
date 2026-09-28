<script lang="ts">
  import { login } from "./auth";

  export let onSuccess: () => void;

  let password = "";
  let error = "";
  let busy = false;

  async function submit(): Promise<void> {
    busy = true;
    error = "";
    try {
      await login(password);
      onSuccess();
    } catch (err) {
      error = (err as Error).message;
    } finally {
      busy = false;
    }
  }
</script>

<div class="login-wrap">
  <form class="panel login-panel" on:submit|preventDefault={submit}>
    <h1>Web-TRX</h1>
    <p class="hint">Einzelner Betreiber-Zugang -- geteiltes Passwort.</p>
    <div class="field">
      <label for="password">Passwort</label>
      <input id="password" type="password" bind:value={password} autocomplete="current-password" />
    </div>
    {#if error}<div class="error">{error}</div>{/if}
    <button class="primary" type="submit" disabled={busy || !password}>Anmelden</button>
  </form>
</div>

<style>
  .login-wrap {
    display: flex;
    align-items: center;
    justify-content: center;
    min-height: 100vh;
  }
  .login-panel {
    width: 300px;
    display: flex;
    flex-direction: column;
    gap: 12px;
  }
  .hint {
    margin: 0;
    color: var(--text-dim);
    font-size: 0.75rem;
  }
  .error {
    color: var(--danger);
    font-size: 0.8rem;
  }
</style>
