import { svelte } from "@sveltejs/vite-plugin-svelte";
import { defineConfig } from "vite";

// Dev-server proxy so the browser can talk to the FastAPI backend (see
// backend/web_trx/server.py) without a separate CORS setup -- matches how
// this will sit behind a single reverse proxy in production too (see
// docs/PROJECT_PLAN.md section 3).
export default defineConfig({
  plugins: [svelte()],
  build: {
    // AudioWorklet modules (src/lib/worklets) must stay real files: small
    // assets are otherwise inlined as data: URLs, which audioWorklet.addModule()
    // doesn't reliably accept.
    assetsInlineLimit: (file: string) => (file.includes(".worklet.") ? false : undefined),
  },
  server: {
    proxy: {
      "/ws": { target: "ws://127.0.0.1:8321", ws: true },
      "/health": "http://127.0.0.1:8321",
      "/login": "http://127.0.0.1:8321",
      "/logout": "http://127.0.0.1:8321",
      "/session": "http://127.0.0.1:8321",
      "/tx-log": "http://127.0.0.1:8321",
    },
  },
});
