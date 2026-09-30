import { defineConfig, type ProxyOptions } from "vite";
import react from "@vitejs/plugin-react";

// The backend (FastAPI, `uv run kalshibot serve`) listens on :8765 and serves the
// built `frontend/dist/` at `/`. During development Vite proxies `/api` (REST + the
// SSE stream at /api/stream) to it.
//
// Routing uses the HTML5 history API (real paths such as /strategies or
// /backtests/42), so assets are referenced with the absolute base "/" and the backend
// MUST fall back to dist/index.html for every non-/api GET that is not a real file
// (see README.md, "Serving from FastAPI").
// Override with KALSHIBOT_API_URL when the backend listens elsewhere, e.g.
// `KALSHIBOT_API_URL=http://127.0.0.1:9000 npm run dev` next to `kalshibot serve --port 9000`.
const apiTarget = process.env.KALSHIBOT_API_URL || "http://127.0.0.1:8765";

const apiProxy: Record<string, ProxyOptions> = {
  "/api": {
    target: apiTarget,
    changeOrigin: true,
    // /api/stream is a long-lived SSE response; never time it out in the proxy.
    timeout: 0,
    proxyTimeout: 0,
  },
};

export default defineConfig({
  base: "/",
  plugins: [react()],
  server: { host: "127.0.0.1", port: 5173, proxy: apiProxy },
  preview: { host: "127.0.0.1", port: 4173, proxy: apiProxy },
  build: {
    outDir: "dist",
    emptyOutDir: true,
    sourcemap: false,
    chunkSizeWarningLimit: 900,
    rollupOptions: {
      output: {
        // Split vendor code so app updates don't invalidate the big chart bundle.
        manualChunks(id: string) {
          if (!id.includes("node_modules")) return undefined;
          if (/[\\/]node_modules[\\/](react|react-dom|scheduler|react-router|cookie|set-cookie-parser)[\\/]/.test(id)) return "react";
          return "vendor";
        },
      },
    },
  },
});
